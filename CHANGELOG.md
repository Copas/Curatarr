# Changelog

## Unreleased

## 0.2.0rc57 (2026-10-30)

- **Fixed: a few stray matches could get good subtitles set aside.** Each stretch listened to was measured by the middle of all its matched lines, so in a quiet stretch two chance matches far away (with only two real ones) made subtitles that were in sync look 15 seconds out (Beetlejuice Beetlejuice). A stretch is now measured by its largest group of lines that agree with each other; a real cut, where most lines agree on a new offset, still shows. Files set aside before this are put back and checked again once more.
- Re-timed subtitles keep their name (`….en.srt`), so Bazarr still counts them as English and doesn't download them again; Bazarr only recognises names that end in the language.

## 0.2.0rc56 (2026-10-30)

- **Fixed: subtitles only a little uneven were set aside.** Subtitles within a couple of seconds everywhere, but not by a single steady amount, were counted as unfixable, set aside and replaced (about half of those set aside so far, Moana among them). Subtitles are now set aside only when they're more than 3 seconds out somewhere; closer ones are left as they are ("close enough"). Files set aside before this are put back on their own and checked again, and Curatarr's own subtitles for them are removed if unchanged (they're made again from the saved transcript if the check still says so).
- **Fixed: re-timing failed with "Operation not permitted"** on network shares where Curatarr can't copy a file's dates. The original is now copied by its contents only. Those files are tried again.
- **Fixed: one stretch heard as another language stopped the check** (a Scottish or Northern English accent heard as Welsh, a scene in French). Most of the stretches listened to must be another language before a film counts as not English; files skipped that way are tried again.

## 0.2.0rc55 (2026-10-30)

- **Enhance → Subtitles**, a new tab, shows what happened: how many subtitle files were checked, in sync, re-timed, set aside or couldn't be told, how far through the library the background check is, and each re-timed or set-aside file with what was wrong. Buttons work in place: **Undo** a re-timing, **Bring back** a file set aside, **Check again**, **Check all again**, *Make subtitles for N without*, and **Make Curatarr's own for every file** (asks first; any English ones are kept beside them). What you undo or bring back stays that way until the file changes. Progress counts subtitles re-timed.

## 0.2.0rc54 (2026-10-30)

- **Out-of-sync subtitles are fixed.** Downloaded English subtitles are often timed for another release of the same film: a different opening shifts every line, and a different frame rate (24 against 23.976, 25 against 24) makes them drift further out as it goes on. Curatarr used to see "already has English subtitles" and leave such files alone. Now a subtitle job listens to four short stretches of the film (about ten minutes in all) and matches what's said to the subtitle lines. Subtitles off by a steady delay or drift are re-timed to the speech, with the original kept beside them as `.unsynced`. Ones that can't be fixed that way (another cut of the film, or another film) are set aside as `.out-of-sync`, and Curatarr makes its own. Removing Curatarr's puts them back. Too little speech to tell, or audio that isn't English, leaves the file as it is. Only SRT files are checked, and each is checked again only if it changes. Subtitles the library already had are checked in the background, a few at a time. On by default: Settings → Enhance → Subtitles, *Fix English subtitles that are out of sync*.

## 0.2.0rc53 (2026-10-30)

- **Pictures made with ChatGPT start from the film's own artwork, not its plot.** The prompt gave the plot as the picture's "key imagery", and the model illustrated it (John Wick's dog turned up on his poster), while the original art was only a "tonal reference". Now, by default, each picture is a new version of the title's own picture of that type (its poster, background, logo…, from Jellyfin), keeping what makes it recognisable (concept, composition, mood, palette, era, iconic imagery, lettering), with your cast in place of the actors, each in the costume of the character they replace, and nothing added that the artwork doesn't have. The plot isn't sent. **Start from** (Generate, Batches, and the Posters page for *Make them with ChatGPT*) can choose *A new scene from the story* instead, and titles without original art always get one; then the story gives only mood and setting, never plot details to draw.

## 0.2.0rc52 (2026-10-30)

- **Review without reloading the page.** Approve, Turn down and Make it again (and a job's Approve the whole set, Cancel, Retry and engine change) now work in place: a short note confirms what happened, and the page doesn't reload. On Review, a picture you've dealt with folds to a one-line note and the next moves up, so going through many is quick; on a job's page, just that picture is redrawn. If the background request fails, the button works the usual way.

## 0.2.0rc51 (2026-10-30)

- **Fixed: Overview reloaded the whole page every 15 seconds during a reconciliation**, closing anything you'd opened and jumping back to the top. Only its status line updates now, in place; when the run finishes, the line says so with a link to show the new numbers.
- Enhance's activity card is only redrawn when something in it changed, and keeps your place on the page. No page in Curatarr reloads itself any more (a test now checks every page).

## 0.2.0rc50 (2026-10-30)

- **Fixed: job pages closed whatever you'd opened every few seconds.** While pictures were being made, Jobs and a job's page reloaded their contents every five seconds, closing any section you'd opened and undoing ticks. They now ask Curatarr what changed and update only that, without reloading: progress bars move in place, and a picture or a job's row is redrawn on its own only when it changes (a new picture, a new state), keeping what was open, ticked or typed in it.
- **"Put on for" starts from who has the picture now.** When a title already has a custom picture of that type, the new one starts ticked for exactly the same people (the accounts that have one, and everyone only if there's one for everyone), so replacing it changes nothing about who sees what unless you choose to. Without one, it's whom the job was made for.

## 0.2.0rc49 (2026-10-30)

- **Fixed: "Use each title's original art" usually sent nothing.** Curatarr looked for the art only beside the title's file, and most libraries keep it in Jellyfin's own metadata folder, so the AI got no original poster or logo to take the feel and lettering from. It now asks Jellyfin when the art isn't beside the file: the picture from before any custom one for everyone, or Jellyfin's own where only some accounts have their own (never your own art passed off as the original). Codex is given the title's original poster, logo and picture of the type it's making; a local model the logo, for the lettering.
- **Review is quicker to work through.** Each picture sits large beside the versions people see now, with **Put on for**, **Approve and put on**, **Turn down** and **Make it again** right under them. What it was given (folded, with a one-line summary such as "4 photos of Amber, 4 photos of Jon, the set's poster, 3 pieces of original art", or a note when no original art was found) and how it was made are folded away until you open them. Every picture opens full screen, references and Jellyfin's own included.

## 0.2.0rc48 (2026-10-30)

- **Review shows every version in use, and you choose who gets the new one.** Each picture under Review (and on its job's page) now sits beside every version of it people see now, captioned with who sees each (Jellyfin's own for everyone else, and each account's own, accounts with the same picture together), instead of only one. **Put on for** ticks choose who gets the new one, everyone and/or any accounts, starting from whom the job was made for; *Approve the whole set* has the same ticks. Putting a new version on for some accounts leaves the version others see as it is.

## 0.2.0rc47 (2026-10-30)

- **Generate, Batches and Benchmarks remember your choices.** Each form starts from what you chose last time, kept for you on the server (so on every device): the picture types, who they're for, the engine and worker, the photos and who's in them, the look, and the box allowing the photos to go to an outside service, so it's ticked once and stays ticked.

## 0.2.0rc46 (2026-10-30)

- **See each picture as soon as it's made.** Posters → Jobs shows a small preview of every finished picture beside its job, as it arrives (the page updates itself while anything's being made); click one to review it on the job's page.
- A set's other pictures say plainly that they're waiting for the new poster the job is making, which they're made to match.

## 0.2.0rc45 (2026-10-30)

- **Fixed: a job could wait forever without saying why.** With only Codex on offer, a job left at *Best available* sat at "Waiting", even with its photos allowed to go out. Best available now uses an outside service when the job allows it and nothing on your own computers can make it, and Generate chooses Codex when it's the only engine.
- **Every waiting picture says why** on Jobs and its job's page (no worker online, the engine not ready, a usage limit until a time, the set's poster first, Curatarr paused, a paused batch…), and a waiting job's engine can be changed on its page.
- **Fixed: a worker asking for work while Curatarr was busy got a server error** ("database is locked"). It's now told to come back shortly, and a worker marks itself seen at most once a minute instead of at every request.

## 0.2.0rc44 (2026-10-29)

- **Setting up a worker is one paste.** Adding a worker on Posters → Workers now shows a single command, with its token in it, that connects the worker, finds Codex and ComfyUI, and starts it in the background as a user service (now and at every sign-in). The service follows the installer's current release, so it's upgraded with Curatarr.

## 0.2.0rc43 (2026-10-29)

- **Fixed: a worker run as a service couldn't find Codex.** Services don't get a terminal's PATH (Codex usually lives in `~/.local/bin`), so the worker reported Codex as not installed. `curatarr-artwork-worker setup` now saves Codex's full path.

## 0.2.0rc42 (2026-10-29)

- **Make them with ChatGPT, without copying prompts.** Posters → Artwork has a **Make them with ChatGPT** button beside the prompt writer: your worker sends each picture to ChatGPT's image tool through Codex, signed in on its computer, with the prompt options as saved, and each waits for you under Review. Tested end to end with a household's own photos. When the plan's image allowance runs out, the rest wait for it to reset.
- **Codex isn't asked again by itself.** Text recognition can't read thin, spaced-out lettering, and each retry used the plan's allowance; ChatGPT's pictures now take one try each (the checks still show in review). Local models still try again when a title's misspelled.

## 0.2.0rc41 (2026-10-29)

- **Qwen-Image-Edit, a better local engine for pictures of you.** Tested on a household's own photos and favourite films, FLUX.2 Klein lost people's likenesses and each film's look; Qwen-Image-Edit 2511 (a free download, docs/ARTWORK.md) kept them far better and dresses the cast as the film's own characters. When it's installed, it's used for pictures with people.
- **Reference photos are used more carefully**: each is cropped to the head (whole photos had their clothes, sunglasses and T-shirt slogans copied onto the posters), and each person's most typical photos are the ones used, so a photo in sunglasses or heavy make-up isn't. Curatarr now sends a worker all of a person's photos (up to five) to choose from. Prompts ask for the story's own characters, in their costumes.

## 0.2.0rc40 (2026-10-29)

- **Compare generated pictures with the ones you have** (Posters → Compare). A title's picture as it is now (made with ChatGPT, say) and one a local model made for it, side by side in random order: choose the better with ← and →, and see which was which after. Your choices are tallied by picture type and engine, and the new ones you preferred are listed, to put on from their job's page.
- **Make local alternatives**: a batch can now make new pictures for titles that *have* them (newest sets first), to compare, without replacing anything. The Posters page's AI section can also hand its prompt options straight to your workers (**Generate these locally**, **Make local alternatives to compare**) instead of writing prompts to copy.
- **Better likeness from reference photos where the face is small**: the worker crops such photos to head and shoulders before the model sees them. Banners are now made by Krea 2 by default (FLUX.2 kept putting people in them).

## 0.2.0rc39 (2026-10-28)

- **Generate artwork with AI, from Curatarr itself** (Library → Posters → Generate). Choose titles and picture types, and a **worker** makes them: a program on this computer or another (it only connects to Curatarr, with its own token, so it needs no open port or shared folders), running a local image model through ComfyUI (FLUX.2 Klein, Krea 2, SDXL with IP-Adapter FaceID) or Codex's image tool with the computer's own ChatGPT login. Reference photos cast your household as the leads, a set's other pictures are made to match its poster, logos come out truly transparent, and each title is read back and made again when it's misspelled. **Nothing changes in Jellyfin until you approve a picture**, which then goes on like an imported one (original kept). Pictures can be approved one by one or as a set, made again with notes on what to change, or turned down. **Batches** make a library's missing artwork a few titles at a time, with a preview first, pause, resume and retry; **Benchmarks** compare engines on the same made-up films, with measured checks, your scores and blind A/B choices. Reference photos only go to an outside service (Codex) when you allow it. Setup: docs/ARTWORK.md. The copy-and-paste prompts still work as before.

## 0.2.0rc38 (2026-10-28)

- **Sort the Posters page and the Gallery.** Newest first (as before), oldest first, or by title A–Z or Z–A. Each person's choice is remembered, is shared by both pages, and sets the order the Gallery's title pages step through.

## 0.2.0rc37 (2026-10-27)

- **Fixed: the installed app reported the wrong version.** The published v0.2.0rc36 tag still packaged v0.2.0rc34, and the install instructions named that older tag. This release gives the package and install commands the same version.

## 0.2.0rc36 (2026-10-27)

- **Enhance says when it's waiting for Jellyfin.** While Jellyfin remakes the scrubbing thumbnails of files Curatarr rebuilt, the next rebuilds wait, and the activity panel said "Idle: nothing is queued" beside hours of work left, as if nothing were happening. It now says it's waiting for Jellyfin, for how many files.

- **Fixed: an out-of-range Library title page said no titles had been discovered**, and a very large page number caused a server error. Browse titles now shows the last available page.

## 0.2.0rc34 (2026-10-27)

Found by end-to-end testing: every page and form, odd input to every route, a copy of a real library, and a week of a live installation's logs.

- **Fixed: some shows' title pages failed to open** ("Internal Server Error"). Jellyfin lists a few episodes without a season or episode number, and sorting them beside numbered ones broke the page; the same could break the hourly always-keep fill, Leave after watched and a few labels. They now sort first and show as "S?E05".
- **Fixed: background jobs often failed with "database is locked"**, and a Jellyfin webhook could be lost. The hourly reconciliation kept one transaction open for the whole library (minutes, while Sonarr was asked about every show), and refreshing a title in Jellyfin after a file change held SQLite's write lock for the whole call; other writers gave up after 30 seconds. Each title is now saved as it's read, and changes are saved before Jellyfin is called.
- **Fixed: Enhance's live activity could fail** while a subtitle check was being saved.
- **Faster Enhance and Problems pages** (about 3.5 s to 1 s on an 8,000-file library): the lists of files waiting for Fix were read file by file.
- **A clear message when the secret key doesn't match**: a database used with a different `CURATARR_SECRET_KEY` (a restored backup, a changed config) made every page fail; it now says what's wrong and how to put it right.
- **Fixed: demo mode** failed on Search once and when writing picture prompts, and anything else the demo doesn't pretend to do now answers as an unreachable server would.

## 0.2.0rc33 (2026-10-26)

- **Each title has its own page in the Gallery**: its background fills the top of the page with its logo over it, then its poster, wide tile and banner, large, each opening full screen. ‹ › (or the arrow keys) step to the titles before and after it, **Slideshow** plays its pictures, and *Next* at the bottom leads on. A title's card on **Sets** opens it.

## 0.2.0rc32 (2026-10-26)

- **Posters → Gallery**: every title's custom pictures laid out to show people. **Sets** shows each title as a hero card (its background, with its poster and logo over it); **Backgrounds**, **Posters** and **Wide tiles** show every picture of that type on its own. Any picture opens full screen, with the arrow keys, swiping or the buttons to look through the rest, and **Slideshow** plays them by themselves.

## 0.2.0rc31 (2026-10-26)

- **Fixed: Household choices and Automatic were missing from More**, so on a phone they could only be reached through other settings pages' tabs.

## 0.2.0rc30 (2026-10-26)

- **Find Favorites moved to Settings → Automatic**, with the other things Curatarr does by itself (off by default). Off, the films are listed on Library → Collections to add by hand. A choice made on Household choices in 0.2.0rc29 is carried over.

## 0.2.0rc29 (2026-10-26)

- **Find Favorites** (off by default; in Settings → Automatic from 0.2.0rc30). When anyone favourites a film that's part of a series (a TMDB collection in Radarr), Curatarr adds the series' other released films to Radarr with the favourite's quality profile and folder, and searches for them, a few each hour. Films excluded in Radarr, hidden on the film series page, or added once before are left out; History says which favourite brought each one in.
- **Changed: a new installation starts with every automatic fix off.** The checks still run, and what they find waits on Enhance → Problems with a button to fix it, so nothing is deleted, downloaded again, edited or rebuilt until the owner has seen what Curatarr does and switched that fix on in Settings → Automatic. **Existing installations keep their fixes as they were** (a migration writes down what they did).
- **The default-audio fix and seek repair can ask first.** Files playing a commentary or audio description first, and files Roku can't seek in, are listed on Problems with **Fix**, **Fix all** and **Leave it**, unless their new switches in Settings → Automatic are on.
- **Fixed: Problems' "Files" heading only showed when a show was missing episodes.**

## 0.2.0rc28 (2026-10-26)

- **Search shows the titles straight away**, then adds *What was said* below them as the transcript search finishes, instead of waiting for both before showing anything.

## 0.2.0rc27 (2026-10-26)

- **A title page's pictures open full screen** when clicked, as on the Posters page: its poster (Jellyfin's own or a custom one, and a picture picked for upload before it's saved) and each custom wide tile, background, logo, banner and per-account picture listed beside it. A click anywhere or Esc closes it.

## 0.2.0rc26 (2026-10-26)

- **Picture prompts point the AI to each title's original art** (on by default: *Use each title's original art as a reference*). Each title lists its original poster, background, wide tile, logo and banner found beside its file, and the AI is asked to carry over their mood, colour palette, era, genre feel and visual motifs, taking the feel rather than the layout and none of the actors' faces. Written from the story alone, the pictures lost the feel of the film.

## 0.2.0rc25 (2026-10-26)

- **Changed: shows Sonarr has unmonitored are treated like any other.** Curatarr used to leave them alone (unless it had trimmed them itself), so a show added unmonitored never got its always-keep episodes or the next season as you watched. Now a request monitors the show and only the episodes it asks for, so Sonarr still doesn't download the rest. Shows just added to Sonarr are taken on before Jellyfin lists them whether they're monitored or not. Daily shows are still left alone.

## 0.2.0rc24 (2026-10-26)

- **Fixed: a show just added to Sonarr never got its always-keep episodes.** Curatarr learned of shows only from Jellyfin, which lists a show once it has a file, and the always-keep fill is what would have fetched the first one. A show added to Sonarr in the last 30 days, monitored and with nothing downloaded, is now taken on from Sonarr straight away, in the TV library holding other shows from its Sonarr root folder (or the only TV library), and filled on the next hourly check. Once Jellyfin lists it, it's the same title.

## 0.2.0rc23 (2026-10-26)

- **Removed: the nudity index** (added in 0.2.0rc20). On real films it missed scenes with nudity and flagged clean ones (a shirtless man, a woman at a kitchen table), and neither a larger reading nor a second opinion from a local vision model made it trustworthy. Its setting, the Fun facts section, the card on film pages and the stored results are gone; the `nudity` install extra is dropped from remembered extras on upgrade.

## 0.2.0rc23 (2026-10-26)

- **Removed: the nudity index** (added in 0.2.0rc20). Tested further, its detector missed real scenes and saw ones that weren't there about as surely, so its rankings and timelines couldn't be trusted. The setting, the Fun facts section, the card on each film's page and what it had stored are gone; installs that had the `nudity` extra drop it on upgrade. The downloaded detector (`models/nudenet-640m.onnx` in the data folder) can be deleted.

## 0.2.0rc22 (2026-10-26)

- **Fixed: Fun facts could miss the first nudity rankings for ten minutes.** A visit before the first film was read remembered "nothing yet"; the rankings now show as soon as there's one.

## 0.2.0rc21 (2026-10-25)

- **Fixed: Settings → Enhance failed to open** ("Internal Server Error") in 0.2.0rc20: an apostrophe in the nudity index's description broke the page.

## 0.2.0rc20 (2026-10-25)

- **The nudity index** (Settings → Enhance → Nudity index, off by default; the `nudity` install extra). Curatarr reads each film a frame a second with NudeNet's small detector, checks every frame it flags with NudeNet's large one (downloaded once, 100 MB), both on the server, and notes when bare breasts, full-frontal female nudity and bare bottoms first appear, how many scenes and how long. Tested on twelve films with and without nudity. No picture is kept. One film at a time, about three minutes each, never while Jellyfin is transcoding; animated films and those rated G, PG or Approved are skipped. For administrators: a Nudity section on Fun facts (soonest, most, nudity an hour, films with none) linking to each film's page, where a Nudity card has the details and a timeline.

## 0.2.0rc19 (2026-10-25)

- **The cast is in some backgrounds.** The AI prompts allowed people in a background only if their faces were large enough to recognise, so the agent always left them out. Now about half the titles' backgrounds, where it suits the story, have the cast on the right-hand side, faces large, clear and well lit, with the lower left still kept calm for the title and plot.

## 0.2.0rc18 (2026-10-25)

- **The AI prompts ask for the lettering to be made with the picture.** In the first sets, every title (on the wide tile, banner and logo alike) was in the same plain serif font: the agent made the pictures without text and typed the titles on with code. The prompts now say to make the lettering with the image tool, as part of each picture, the logo with a transparent background.

## 0.2.0rc17 (2026-10-25)

- **The AI prompts ask for the title's own logo lettering.** Asked only for "lettering in the film's style", logos came out in plain fonts. The title, on the logo and wherever else it appears, is now in the lettering of the film's official logo: the same typeface (or the closest match), weight, letter shapes, spacing, colours and effects. The prompts no longer say the pictures mustn't copy the official artwork.

## 0.2.0rc16 (2026-10-25)

- **The AI prompts no longer ask for role portraits.** To keep faces the same across a title's set, they asked the agent to make a head-and-shoulders portrait of each person in costume first: extra pictures Jellyfin never uses, kept in a folder the agent made up. A title's poster already shows them in costume for that story, so it's the reference instead: each wide tile, background and banner is made from their photos plus the poster, and a poster that's still to be made is made and checked first.
- **The AI prompts ask for an extreme take by default**: a new *The look* box on Library → Posters, filled in with an over-the-top movie-poster take (big, intense expressions: terror, joy, rage, romantic or sexual tension, bold composition and lighting), which goes into every prompt. Change it, or empty it for none.
- **The AI prompts ask for only the files listed**: no drafts, test images, extra versions or working references, as image generation is slow and costly and Jellyfin uses nothing else; a picture is made again only when it comes out wrong.

## 0.2.0rc15 (2026-10-25)

- **Fixed: updating to plugin 0.8.0.0 fetched every wide tile again.** A wide tile made from an account's poster was listed as a picture of its own, under a new tag, so the plugin downloaded all of them again (about 700), each drawn afresh, while it already had them. It's the poster's wide version again, as plugin 0.7.0.0 had it, so those files are kept; an account's own wide tile still takes its place. With plugin 0.8.1.0, pictures it already has show straight away after Jellyfin restarts.

## 0.2.0rc14 (2026-10-25)

- **Pressing play moves a file to the front of the line.** What's playing, and the five episodes after it (into the next season too), go first in every queue within seconds: chapters, subtitles, skip markers, TV-friendly audio and the Roku seek index repair, in watching order. Before, the queue only learned what people watch next every ten minutes, from Jellyfin's Next Up and Continue Watching, with two episodes after each, and the seek repair didn't use it at all (it went by when anything in a show was last played, then file order).
- **Fixed: the Roku seek index repair gave up on files with their audio first.** mkvmerge put the video track first, so the check found the copy different from the original and kept the broken file: 201 files (Good Eats, Impractical Jokers, Top Gear and others) still couldn't be seeked on a Roku. It now keeps the file's own track order, and those files are tried again.

## 0.2.0rc13 (2026-10-25)

- **The AI prompts use every reference photo.** They asked for each person's two or three best photos, so the same ones led every picture; the photos are different on purpose. Now each picture leads with the photos that suit it (a different one each time), and photos with a hidden or small face are paired with a clear one.
- **Plugin updates can install themselves** (Settings → Curatarr plugin for Jellyfin → *Install plugin updates automatically*, off by default): when Jellyfin's catalogue has a newer plugin, Curatarr installs it and restarts Jellyfin to load it once nobody has watched anything for 10 minutes, as the banner's buttons do. History records each one.
- The plugin's release can be built again by hand from GitHub Actions for an existing tag, and its builds get .NET from Microsoft's CDN: the usual download server refused every version for hours, so plugin 0.8.0.0's release failed.

## 0.2.0rc12 (2026-10-25)

- **The AI prompts keep the people in your reference photos recognisable.** Cast in place of a story's actors, they came out as generic faces with their general look, or small and turned away. The prompts now ask the agent to pick each person's clearest photos (no sunglasses, nothing far away), make an approved head-and-shoulders portrait of each of them in costume first, build every picture from images rather than a description, keep their own faces, age, build, hair and beards, show faces large and clear (a background or banner goes without people rather than with unrecognisable ones), and compare each face with the photos before saving.
- A reference photos folder that isn't there gets a warning when the prompt options are saved.

## 0.2.0rc11 (2026-10-25)

- **Fixed: chapter and subtitle jobs paused themselves while someone watched.** While Jellyfin was transcoding, Curatarr counted the graphics card's whole load against its target, its own transcription included, so a single subtitle job pushed the GPU to 90%, paused itself, and resumed once it fell back, over and over, with the machine mostly idle. Curatarr's own share (its processes, from `nvidia-smi pmon`) is now left out, as Jellyfin's transcoding runs on the card's own encoder and decoder; only a card that's all but full (98%) counts it again. The Activity card says what it left out ("GPU 12% besides Curatarr's own 78%").

## 0.2.0rc10 (2026-10-25)

- **The AI prompts ask each title only for what it's missing.** With *Skip titles that have them all*, a title that has its poster but not its other pictures was asked for the whole set again, poster included. Now it's asked only for the pictures it lacks, and where its poster is in the import folder the prompt points the AI to it, to match the rest to its cast and style; the new files are named after it, so they sit beside it.
- **Fixed: the AI prompts could include deleted titles.** Titles with no file on disk are left out.

## 0.2.0rc9 (2026-10-25)

- **Every picture of a title, not just its poster.** Custom pictures can be a title's wide tile (the landscape picture in rows such as the Roku app's home rows and Continue Watching), background, logo or banner as well as its poster: uploaded on a title's page (now **Pictures**, choosing which), or imported with a word before the extension (`….thumb.jpg`, `….backdrop.jpg`, `….logo.png`, `….banner.jpg`). Each is for everyone or for chosen accounts, with the original kept to restore; a logo stays a see-through PNG. Library → Posters groups each title's pictures in one tile.
- **The AI prompts can ask for a matching set**: tick the pictures to make, and each title gets the same cast and style across them, each a different scene, with the file name for each. *Skip titles that have them all* skips a title only once it has every picture asked for.
- **Without a picture of its own** (Library → Posters): for an account with its own poster or a ribbon, a wide tile made from its poster (the default, as in 0.2.0rc8) or Jellyfin's own; a background of the poster blurred, or Jellyfin's own (the default); a banner made from the poster, or Jellyfin's own (the default).
- An account's own pictures other than the poster need plugin 0.8.0.0; the banner on every page says when it's out.
- **Click a picture on Library → Posters to see it full screen**; click again or press Esc to close it. The title's name still opens its page.
- **Fixed: a deleted title's custom pictures stayed behind.** Its posters stayed active, Curatarr kept their copies, and the plugin kept listing and storing them. Now, once a title is gone (deleted by Curatarr or outside it, or no longer in Radarr or Sonarr), its custom pictures are retired and Curatarr's copies deleted, and History says so; the plugin drops its copies within a minute. Files in the import folder are left alone.

## 0.2.0rc8 (2026-10-25)

- **Custom posters and favourite ribbons show in rows of wide tiles too**, with plugin 0.7.0.0. Some rows (the Roku app's home rows, *Recently added in My List*, Continue Watching) show a title's landscape picture rather than its poster, so an account's own poster and its ribbon didn't show there. Curatarr now draws a landscape version, the poster at full height on a blurred copy of itself, and the plugin shows it in those rows to the same accounts.

## 0.2.0rc7 (2026-10-25)

### Keeping and letting go

- **Free-space thresholds can be a size** (Retention rules → *Low/Critical free-space threshold as a size*), such as "keep 200 GB free", used instead of the percentage where set. On a 10 TB array one whole percent is about 100 GB, too coarse a step. Cleanup, its reasons, the forecast on Overview and the cleanup preview all use it. ([#2](https://github.com/Copas/Curatarr/issues/2))
- **Leave after watched** (a title's page → Flags, or the API). For one last watch: nothing starts when it's set, and once the film (or a show's last episode on disk) has been watched, by the account you choose or by anyone, it's marked Leaving Soon with the usual notice and rescues. Before, marking it Leaving Soon and then watching it rescued it, so you had to remember to mark it afterwards. ([#1](https://github.com/Copas/Curatarr/issues/1))
- **Leave after watched for a season or one episode, and episode by episode** ([#1](https://github.com/Copas/Curatarr/issues/1)). A show or season can go once its last episode on disk is watched, or each episode as it's watched; a single episode once it's watched (from the title page, or the API with a season's or episode's Jellyfin id). Those episodes leave on their own: the show stays, its poster isn't badged, watching on through the show doesn't rescue them (playing one of them again does), their files are deleted through Sonarr and unmonitored first so they don't come back, and the always-keep episodes never go. Removing the whole show takes over from them.
- **Posters say a title is waiting for a watch**: with the Curatarr plugin, a film or show flagged Leave after watched (or an episode's thumbnail) has an amber "LEAVING AFTER YOU WATCH" tag for the account whose watch counts, or everyone when anyone's does. No plugin update is needed for it. ([#1](https://github.com/Copas/Curatarr/issues/1))
- **Leave after I watch in Jellyfin's web menus**, with plugin 0.6.0.0: for a film, show, season or episode, counting the watch of whoever chose it; **Don't leave after watched** undoes it. ([#1](https://github.com/Copas/Curatarr/issues/1))
- **Never compress** for an external transcoder such as Tdarr: flag a film, show, season or episode, or protect favourites automatically, and Curatarr lists those files at `/api/v1/protected-paths?kind=compress` and, optionally, in a file, for the transcoder's skip list (Settings → Never compress). ([#1](https://github.com/Copas/Curatarr/issues/1))
- **A warning when a Never compress file changes** (Settings → Never compress, and History): its size as Sonarr or Radarr report it is checked at each reconciliation, so a transcoder that ignored the skip list shows up. ([#1](https://github.com/Copas/Curatarr/issues/1))
- **Compression first** (Settings → Compression first, off by default): while an external transcoder such as Tdarr has files queued, being low on space marks nothing Leaving Soon, so recompressing wins the space back before anything is deleted; below the critical threshold cleanup goes ahead. The transcoder reports its queue to `POST /api/v1/compression` with the webhook token, or keeps a file in place while it has work; a signal goes stale after 6 hours. ([#3](https://github.com/Copas/Curatarr/issues/3))
- **A small JSON API for remotes and Home Assistant**: `GET`/`POST /api/v1/titles/by-jellyfin/<itemId>` reads a film's, show's, season's or episode's state and makes the household's choices (Keep, Snooze, Never Purge) or sets the flags, with the webhook token. ([#1](https://github.com/Copas/Curatarr/issues/1))

### Favourite ribbons

- **Favourite ribbons** (Library → Posters, off by default, with the Curatarr plugin for Jellyfin). A gold tag on a title's poster saying whose favourite it is ("★ Sam", "★ Sam + 1"), shown to the accounts you choose: one tick per account for "everyone sees their favourites", or, under *Choose account by account*, a grid of who sees whose (it opens by itself when choices are mixed), and whether accounts see their own (off); along the bottom (the default) or top right, opposite Leaving Soon's tag; names or just a star. It's drawn over whatever poster that account would see, its own custom poster and the Leaving Soon countdown included. No plugin update is needed.
- **Favourite ribbons show who, what's new, what everyone loves, and episodes.** Each ribbon has the round pictures of who favourited it (their Jellyfin profile picture, one uploaded on Library → Posters, or their initials); a red NEW for 7 days after something's favourited; a red "♥ Household favourite" band when 3 or more people favourited it; and favourite episodes get a ribbon on their thumbnails. Each can be switched off or adjusted; no plugin update is needed. Curatarr now records when each favourite was made, and which episodes people favourited.
- **Favourite ribbons are simpler in lists, with names where there's room, and have looks to choose.** In rows of posters a ribbon is now a star and the faces of who favourited it, without the name beside them. On a title's own page, where the poster is big, it lists each full name under a star, one under another (with plugin 0.5.0.0). Library → Posters → Favourite ribbons has *In lists* (faces, "★ Sam", or just a star), *On its page* (names, or as in lists) and *Colour* (gold, silver, teal, purple), with a preview of each. Earlier settings carry over.
- **Favourite ribbons can go along the top**, clear of a film's title at the bottom of most posters. A Leaving Soon countdown stays visible, with the ribbon just below it.
- **The ribbon previews use a real poster** (the latest favourite's) instead of a grey box.
- **Fixed: an account's own poster hid the Leaving Soon countdown.** Everyone else saw "7 DAYS LEFT" on Jellyfin's poster, but an account with a custom poster of its own saw it without the banner, so it couldn't tell the title was going. Curatarr now draws the banner on the account's poster too, with a new tag each time its words change so apps fetch it again; no plugin update is needed.

### Accounts, the Jellyfin plugin and contributors

- **A banner says when the Jellyfin plugin has an update**, on every page for admins: **Update** has Jellyfin download it, and then **Restart when nobody's watching** restarts Jellyfin to load it once nothing has played for 10 minutes (or **Restart now**). It says when Curatarr's features need the new version.
- **Fixed: a plugin update waiting for a restart switched off what needs the plugin.** Jellyfin lists the version still loaded as "Superseded" until it restarts, which Curatarr took for the plugin being disabled, so AAC tracks for browsers stopped being added and Settings said the plugin was off.
- **Hidden Jellyfin accounts can be kept out of Curatarr** (Settings → Integrations → Hidden Jellyfin accounts). Accounts hidden from Jellyfin's sign-in screen can be shown like any other (the default), left out of Curatarr's pages (never listed or named, while their watching still keeps titles), or ignored completely (not read at all, so a title only they watch can become Leaving Soon).
- **The Jellyfin plugin's version history says what changed.** Each plugin version has notes in this changelog, which Jellyfin shows as the version's changelog and GitHub as its release notes (filled in for every release so far); a plugin release can't be tagged without them. The plugin's description in Jellyfin's catalogue covers everything it does, not only Delete.
- **For contributors:** `AGENTS.md` sets out how changes are made here (tests before every commit, changelog entries, commits under the author's own name, keeping the repository anonymous, releases), for people and coding agents alike. GitHub now runs the tests and linters on every push, and checks the Jellyfin plugin's version has notes as soon as it changes.

## 0.2.0rc6 (2026-10-24)

### Custom posters

- **Custom posters** (Library → Posters). Upload a picture on a title's page (Poster), or have Curatarr pick up pictures from an import folder, matched to titles by their file names (`The Big Lebowski (1998).jpg`, `08_the-big-lebowski.jpg`; a year either side counts). The poster each replaces is kept, and **Restore original** puts it back (a picture that was already showing when imported restores to the provider's poster). Leaving Soon draws its countdown on a custom poster. **Make them with an AI** writes prompts for an image-making AI agent, in batches: which titles (an account's favourites, every film…), each with its year, genres, plot and the file name to save it as in the import folder, and where to find reference photos of people to cast, with their consent.
- **A poster for one account** (prototype; needs the Curatarr plugin for Jellyfin). On a title's Poster panel, choose *Only for* an account: that account sees the picture as the title's poster in every Jellyfin app, and everyone else keeps the usual one. Jellyfin has one poster per title, so the plugin swaps the poster's image tag in what's sent to that account, and serves the picture (resized and cached by Jellyfin like its own) when an app asks for that tag; Curatarr lists the posters for it at `/api/v1/jellyfin/posters`, with the webhook token.
- **Imported posters can be for chosen accounts** (Library → Posters → *Imported posters are for*). Each picture in the import folder becomes those accounts' own poster, and everyone else keeps the usual one: where an imported picture was everyone's poster before, Jellyfin's own goes back on for them (the saved original, or the metadata provider's poster when Curatarr has none). Changing who they're for looks at every picture again.
- **Library → Posters shows one tile per picture**: a poster several accounts have is one tile listing each account with its own Remove, and **Remove for all**. Fixed: the page's title in the browser tab showed a script tag.
- **A poster you pick shows before it's uploaded**, in the title's Poster panel, and the panel says whether the large picture is what everyone sees or what everyone else sees.

### Plays smoothly on every device

- **Each device plays what it can play as it is** (plugin 0.3.0.0). Jellyfin plays the default audio track and a title's first copy whatever the device can decode. The plugin now asks Jellyfin's own stream builder, with the device's profile, how each copy and each audio track in the same language would play, and points the player at the one needing the least work: a browser gets a stereo AAC track instead of AC3 converted on the fly, and of two copies of a film, each device gets the one it plays directly. Tracks and copies picked by hand, other languages and commentaries are left alone.
- **Stereo AAC tracks for web browsers** (Settings → Enhance, on by default with the plugin): added last and not default beside AC3, E-AC3 and DTS, so TVs keep their surround track. TV-friendly audio's rows are now *Playable audio*.
- **AAC tracks go where browsers will play them, not to every file.** Each one rebuilds a file behind Jellyfin's thumbnails, so the whole library would take a month (the estimate said 30 days). Curatarr now notes who watches in which app; for the people who mostly watch in a web browser, their next episodes and what they're partway through get the track, topped up as they go.
- **A commentary or audio description isn't the default track any more** (Settings → Enhance, on by default): the film's own soundtrack in the same language is made the default, in place.
- **Fixed: seeking on Roku started some episodes over.** When chapters (or a track change) didn't fit at the start of an MKV, mkvpropedit moved its seek index pointer to the end, where Roku players don't look. Curatarr now rebuilds such a file straight after editing it (a copy with mkvmerge; nothing re-encoded), and **Seeking on Roku** (Settings → Enhance, on by default) repairs files already like that in the background, what's being watched first.
- **TV-friendly audio no longer forgets a track it added** when the database is briefly busy: the record is retried once, since the file is already rewritten and Radarr then reports the new track.
- **Fixed: TV-friendly audio and the seek repair skipped every unlinked episode** whenever any Jellyfin app was open. An episode not yet linked to Jellyfin has no item id, and an app playing nothing has no now-playing id, so they matched and each looked like it was playing. Idle apps are ignored now, and an unlinked file is matched by its file name.
- **Fixed: TV-friendly audio stalled behind the Roku seek repair.** Both wait for Jellyfin's thumbnails under one limit, and the repair (thousands of files) took every free spot. It now leaves the last spot for audio while audio has work. The overall estimate also treats the two as one queue with a joint pace from the last day, and names work that finished nothing in a day instead of using an older, slower pace (which had said 4 days).
- **File rebuilds wait for Jellyfin's scrubbing thumbnails.** A rebuilt file (TV-friendly audio, the Roku seek repair, removing other languages) is new to Jellyfin, which drops its thumbnails and makes them again, minutes per file. Curatarr was rebuilding far faster, leaving hundreds of titles without thumbnails and the nightly task running for a day. Now it starts another rebuild only while fewer than three rebuilt files are still waiting for theirs (Settings → Enhance → Jellyfin thumbnails, on by default).
- **Rebuilds made while editing chapters or track labels count toward thumbnail pacing too** (when an edit leaves the seek index where Roku can't reach it and the file is rebuilt).

### Leaving Soon and the Jellyfin plugin

- **The Curatarr plugin for Jellyfin** (in `jellyfin-plugin/`, released separately as `plugin-v…`): Delete in any Jellyfin app asks Curatarr first, through a new endpoint, `POST /api/v1/jellyfin/delete`. With the plugin, Delete in any Jellyfin app marks a film or show Leaving Soon instead of deleting its files behind Radarr's and Sonarr's backs. It uses the webhook token.
- **Install the Jellyfin plugin from Settings, in one click**: **Install in Jellyfin** (which says who's watching first) adds the plugin's repository to Jellyfin, installs it and restarts Jellyfin; as soon as Jellyfin is back, Curatarr finds an address Jellyfin can reach it at (tested through the plugin itself) and gives the plugin that and the webhook token. Nothing to type or copy, and it works wherever Jellyfin runs. A plugin installed by hand is connected with **Connect**, no restart. The token is given again whenever it changes, and the repository brings plugin updates.
- **Leaving Soon and Delete, both, from Jellyfin** (plugin 0.2.0.0). Deleting a title that's already Leaving Soon deletes it now, through Radarr or Sonarr, in every Jellyfin app. In the web app, the menu has **Leaving Soon** beside **Delete**, and Delete there deletes straight away; this needs the File Transformation plugin, since only the web app's menus can be changed. Both are for whoever Jellyfin lets delete the item.
- **The Jellyfin plugin has Curatarr's logo** on its card in Jellyfin's plugin list (plugin 0.1.1.0), in place of the generic puzzle piece.
- **A Leaving Soon collection in Jellyfin** (Settings → Household, on by default): titles join when they're marked Leaving Soon and leave when kept, watched or deleted. It's created as "Leaving Soon" and known by its id afterwards, so renaming it is fine.
- **The Leaving Soon collection has a poster**: four of its titles' posters under a red LEAVING SOON band, redrawn when the titles change (Jellyfin draws nothing for a collection made through its API).
- **Leaving Soon posters get a red countdown tag** in the top-left corner, like a streaming service's: "14 DAYS LEFT", then "1 DAY LEFT" and "LAST DAY", redrawn once a day. The film's own title stays visible. Titles awaiting review with no deletion date still say LEAVING SOON.
- **Leaving Soon shows on every picture apps use**: the poster (red tag, top left), the wide home-screen card (shaded red ribbon along the bottom) and the backdrop behind a title's page (a small tag, top right). Each is restored on its own when the title stays.
- **The backdrop's Leaving Soon tag sits lower and further in**, below the header buttons phone apps draw over a title's backdrop and inside the sides they crop.
- **Overview's Leaving Soon summary has a card of its own**, above Needs you. At the foot of the Needs you list it read as part of the row above it (a show listed for misfiled episodes looked like it was leaving).
- **Leaving Soon titles no longer sit in Overview's Needs you.** They're decided (they go unless someone watches), so Overview shows one line instead: how many, how much space, and when the first goes, linking to Review.
- **Fixed: Overview's Leaving Soon line never said more than 5 titles.** It was counted from the upcoming list, which stops at five; it counts every title Leaving Soon now (14 here, not 5).
- **Fixed: a title's page could show an old day count** ("9 DAYS LEFT" on the backdrop beside "8 DAYS LEFT" on the poster). Jellyfin adds an uploaded backdrop to a title's backdrops instead of replacing the first one, so every daily redraw of the tag left the old one behind: titles had around 20 tagged copies (one had 421), apps that rotate backdrops showed whichever came up, and Keep left tagged copies behind. After each upload the other copies of that picture are now deleted and the new one made first, and a one-off pass deletes the copies already there, on restored titles too.
- **Fixed: a Leaving Soon title could be cancelled by viewing from before it was marked.** The hourly check reports plays it finds in Jellyfin, including ones already counted; only viewing after a title was marked cancels its removal now.
- **Fixed: Leaving Soon banners vanished after a Jellyfin metadata refresh** that replaced images. Curatarr checked a poster once and then assumed the banner stayed. It now notices replaced artwork, badges the new poster, and restores to the new art when the title is kept.
- **Fixed: Leaving Soon pictures were re-uploaded again and again** (1,430 times in a day). Jellyfin's tag for an uploaded image changes a while after the upload, which read as the artwork having been replaced, so the badged picture was saved as the original and badged again. The picture itself is compared now. Pictures this affected are repaired by having Jellyfin download the artwork afresh, and restored that way if the title is kept first.
- **Fixed: a Leaving Soon poster could be badged twice at once** when the web app and the worker reconciled artwork at the same moment, which could save the badged poster as the one to restore. Applying a badge now takes a short lease per title.

### What to watch next

- **What people will watch next is got ready first.** The next episodes of shows people are watching, and what they stopped partway through, go to the front of the job queue, so a new episode has its chapters, subtitles and recap marker by the time someone reaches it.
- **Something stopped in its credits counts as watched** (Settings → Enhance → Skip markers, on by default). Jellyfin counts a film as watched only past 90%, and long end credits kept finished films in Continue Watching (Wrong Turn, 9 minutes from the end, its last line 8 minutes before that). When someone stops a film or episode in its credits (Intro Skipper's marker), or after its last line of dialogue with only a few minutes left (twelve for a film, three for an episode), it's marked watched for that person. Anything stopped mid-film, or still playing, is left alone; History lists each one.

### Bad downloads, fixed automatically

- **Automatic fixes are on by default** (Settings → Automatic), since each acts only when it can be confident. Installs that saved the page keep their choices.
- **Episodes Sonarr filed under the wrong episode are put right.** Every half hour the last two weeks of imports are checked for files whose download name carries another episode's title; what's said in the file is compared with each episode's summary. When both agree, the file is moved to the right episode in Sonarr and renamed, and the episode it vacated is searched for again (or unmonitored when scene numbering would repeat the mix-up). A slot that already has a file must hold the same episode before the worse copy goes, and a season shifted by one is fixed one slot at a time, each as soon as the slot before it is free. Anything less certain is listed on Problems.
- **A misnumbered release is blocklisted before the vacated episode is searched for.** When the release itself carries the number of the slot it was filed under (a group's "S03E03" that is really S03E02), searching for that episode downloaded the same file straight back.
- **A replaced film is watched until its new copy arrives.** Replacing a film (no English audio, burned-in subtitles, a broken file) deleted it and asked Radarr once; when that download failed, or the film was unmonitored in Radarr, it was left with no file. Now the film is monitored first, searched again every 6 hours if nothing is downloading (3 searches), put back to how it was when a copy arrives, and Activity says so when none does.
- **A film deleted while it's on one of Radarr's import lists no longer comes straight back.** Curatarr adds a Radarr import exclusion for it, so the list's next sync doesn't add and download it again. Adding it by hand or through Seerr still works.
- **Fixed: episodes were judged "cut short" against the whole show's runtime.** Sonarr lists one runtime per show, but episodes vary (a 22-minute episode of a show listed at 44, a 7-minute Love, Death & Robots short). With automatic replacement on, good copies were replaced over and over: 404 replacements in a week across 101 files, one episode 29 times, each blocklisting a good release. Episodes are now judged by their own listed runtime (not at all when Sonarr has none), and episodes flagged by the old rule are checked again, dropping any Replace waiting on it.
- **Fixed: good copies of a short episode were replaced one after another.** Sonarr lists one runtime for a whole show, so a 32-minute finale listed at 43 looked cut short; with automatic replacement each new copy was flagged and replaced too (six times in a day, every release blocklisted). A copy that runs as long as one already replaced for being short is now accepted (the listing is wrong), and a Replace whose file was already replaced finishes without blocklisting.
- **Fixed: a slow delete made a Replace blocklist each new copy.** Sonarr took over a minute to delete a file from a network share, so the Replace gave up; retried every cycle, it blocklisted whatever had been grabbed most recently (by then the new copy) and failed again, because the file was gone. A file already gone now counts as deleted, deletes may take five minutes, and a retried Replace never blocklists twice.
- **A search for a show deleted from Sonarr stops instead of retrying every hour** (it had failed 96 times). Sonarr's "not found" ends it, and History says the show isn't in Sonarr any more.
- **Fixed: "Reset to minimum" monitored specials that were never downloaded.** Specials are left alone, so they're kept, but monitoring them had Sonarr fetch them for a show that was meant to stay at a few episodes. Only specials already downloaded are monitored again now.

### Subtitles, skip markers and progress

- **Text subtitles for files whose English subtitles are only pictures** (Settings → Enhance → Subtitles, on by default). Blu-ray (PGS) and DVD (VobSub) subtitles are pictures that TV apps such as Roku can't draw, so Jellyfin burns them into the video: turning them on restarts it, it's transcoded, and seeking is slow. Such files now get a generated text track too; the picture track stays. Files checked before (counted as having subtitles) are looked at again in the background, a few each minute; here that's about 57 films and 940 episodes.
- **One overall progress bar** for everything Curatarr is working through (chapter jobs, skip-marker checks, TV-friendly audio, the Roku seek repair): items done and left, and when the slowest should finish, saying which one that is and when its time is rough (rebuilds wait for Jellyfin's thumbnails). It sits at the top of Overview and of Enhance's Activity; the separate bars stay below.
- **Overview's Library care shows the progress bars too**: chapter jobs and skip-marker checks still to go, with about how long.
- **Skip-marker checks get a progress bar** on Enhance with a finish estimate from the last hour's pace. The existing bar only counted chapter jobs, so it vanished once those were done although thousands of checks were still queued.
- **Overview's Library care matches Enhance**: the same rows with the same names, TV-friendly audio split into films and episodes (it was one combined figure), Seeking on Roku added, and the queue labelled as jobs.
- **Enhance's To do buttons say what they'd do.** "Scan N new files" scans only files not scanned yet (new files are scanned on their own anyway), with a separate "Rescan all files" that asks first; "Generate subtitles for N" and "Check skip markers on N more" show how many they'd add and appear only when there's something to add; "already queued" is plain text, not a button-like chip.
- **Skip-marker checks use every free job slot.** They ran in a single slot whatever the job limit, so with thousands queued and few chapter jobs, "up to 5 at a time" meant one. Now one slot is always theirs, chapter jobs come next, and any slots left over take more batches (each with episodes of its own).
- **Fixed: new imports waited behind a subtitle backfill, and the overall bar didn't count subtitles.** Subtitle jobs always ran first, which suited ones that reuse a saved transcript (seconds each) but not the picture-subtitle recheck's, which need transcribing (minutes each): 20 new imports sat a day behind 1,500 of them, and the bar showed only those 20, "measuring the pace". Subtitle jobs that need transcribing now run after chapter jobs, and subtitles are part of the overall bar with their own pace.
- **Fixed: files a library scan had settled never got subtitles or skip-marker checks.** Both are chained after a file's chapter job, and a scan marks files with chapters of their own, too short for chapters, or not MKV as handled, so no chapter job ever ran for them: 825 files here were left "needing subtitles" with nothing queued. The hourly pass that queues new files now queues those follow-ups directly (when subtitles are on for the library, and for the existing library only when that's switched on).
- **Fixed: skip-marker checks could stop entirely.** A chapter job waiting on a file that couldn't be found counted as chapter work in progress, which holds back checks that need new transcription, all of them here, for as long as it waited. Waiting jobs no longer count. And a film or episode Radarr or Sonarr answer "not found" for (removed) closes its job instead of waiting a day for a share that isn't offline.
- **Fixed: the English subtitles bar read 100% while subtitle jobs were still waiting.** Files with a subtitle job queued or running were counted neither as done nor as to do, so 700 waiting files dropped out of the total. They count as still to go now, and Enhance's Needing subtitles shows them with how many are queued.
- **Fixed: the finish estimate read 20 days after an hour of playback.** Subtitle and skip-marker estimates used the last hour's pace, and an hour mostly paused for people watching (3 subtitles done) made 1,486 left look like 20 days. They use the best of the last hour's, six hours' and day's pace now.
- **Curatarr says when a graphics card goes unused.** Settings → Enhance names the NVIDIA card it finds and whether transcription uses it; Enhance shows a notice when an install has faster-whisper but not the CUDA libraries (`EXTRAS=chapters` on a machine with a card), which transcribes several times slower on the processor. Reinstall hints suggest `chapters-gpu` only where there's a card, and `chapters` otherwise. The installer says so too when it finds a card.
- **The job limit's GPU reading says when it isn't counted.** GPU use only limits jobs while Jellyfin is transcoding; otherwise Activity showed, say, "GPU 92% … aiming under 85%: room for 4", which read like a mistake.
- **Work no longer stops for as long as anyone is transcoding.** The graphics card's load was read as the busier of its compute and its video decoder; two Jellyfin transcodes kept the decoder at 97% (compute 15%, CPU 14%), so all work stayed paused though Curatarr's jobs weren't what kept it busy. While Jellyfin transcodes, scene detection now decodes on the processor and the decoder isn't counted; Activity shows it as Jellyfin's.
- **Fixed: the worker could stay paused for good after someone started watching.** Pausing for viewers suspends the tools jobs run, and it suspended one the worker itself had started (a reconcile reading a new import). The worker is what lifts the pause, so it waited on that tool forever: nothing ran for 14 hours, long after playback ended. Only background jobs' tools are suspended now, and a pause left behind by a stopped worker no longer shows as "Paused" on Overview.
- **An unexpected job error says what it was**, with the details under a link, instead of "see the worker log" (which the web app can't show).
- **Fixed: a chapter job failed when the database was busy.** A lock hit during an autoflush (a reconcile holds the write lock for a couple of minutes) surfaced as a different error and was recorded as a failure; the job is put back in the queue instead.
- **Fixed: background jobs failing with "database is locked"** on SQLite. Smart Chapters and skip-marker checks on TV episodes left a change unsaved before reading the rest of the series, which then held the database's write lock through transcription and every Ollama call, often for minutes. File checks, dub checks and the web app's saves failed in the meantime. Changes are now saved before any slow step.
- **Fixed: Enhance's Activity panel failed to load** when a subtitle check listed there was queued again while the page was being drawn (the picture-subtitle pass requeues many at once). It shows "queued again" instead.

### Library, search and dates

- **Library → Free up space**: mark films and shows Leaving Soon by hand, sorted by space freed, untouched longest, oldest or most recently watched, and filtered to watched or unwatched titles. Nobody plays it in the notice period and it goes (shows are trimmed to their always-keep episodes).
- **Free up space** remembers each person's last filter and sort, and can show what one person watched (everyone active in the last 90 days is listed), most recent first.
- **Remove this film** on a film's title page: marks it Leaving Soon on request, with the poster badge and notice period, then deletes it through Radarr unless someone plays it or you choose Keep.
- **Posters in another language are swapped for ones in the metadata language** (Settings → Household, on by default). A new release often has only foreign posters on TMDB when it arrives, and Jellyfin keeps the one it took. Once a day, films and shows released in the last year are compared with TMDB's posters; one in another language is replaced when TMDB has one in Jellyfin's metadata language. Posters chosen by hand and Leaving Soon posters are left alone.
- **Favourites show who chose them**: a gold star with the names of everyone who favourited a title in Jellyfin, on its page, in Review, Free up space, Browse titles and Shows by size.
- **Search results highlight what was searched**: the words of a quoted phrase, and the other search words in any form.
- **Search by what was said: words in "quotes" are an exact phrase**, said as written (whole words, in order). Without quotes, lines with most of the words still match as before.
- **Dates and times read the way people say them**, everywhere they're shown: "today at 5:55 PM", "tomorrow at 4 PM", "Wednesday at 3:21 AM", "last Monday", "Oct 31" (with the year only when it isn't this year's), in the viewer's own time zone and 12- or 24-hour style. Hovering shows the full date and time.

## Jellyfin plugin

The Curatarr plugin for Jellyfin has releases of its own (tagged `plugin-v…`), installed and updated from Jellyfin's plugin catalogue once Curatarr adds its repository. Each version's notes below are what Jellyfin shows as its changelog, and the release page's notes.

### 0.8.1.0 (2026-10-25)

- After Jellyfin restarts, the account pictures already downloaded show straight away, and new ones as they arrive. Before, nobody saw their own pictures until every download had finished, which after the update to 0.8.0.0 took several minutes.

### 0.8.0.0 (2026-10-25)

- An account can have each of a title's pictures of its own, not just the poster: its wide tile (Thumb), background (Backdrop), logo and banner, swapped into what's sent to that account, including a show's background, logo and wide tile on its episodes and seasons. An account's background goes first among the title's backgrounds. A logo is served as a PNG, keeping its see-through background.
- Needs Curatarr 0.2.0rc9 or later for pictures other than the poster; with older ones, it works as 0.7.0.0 did.

### 0.7.0.0 (2026-10-25)

- An account's own posters and favourite ribbons show in rows of wide tiles too (the Roku app's home rows, Continue Watching), where apps use a title's landscape picture (its Thumb) rather than its poster. Curatarr draws a landscape version, and the plugin puts its tag in for the Thumb and serves it.
- Needs Curatarr 0.2.0rc8 or later for the landscape version; with older ones, wide tiles show the usual picture as before.

### 0.6.0.0 (2026-10-25)

- **Leave after I watch** in the web app's menus (with the File Transformation plugin), beside Leaving Soon: for a film, show, season or episode Curatarr knows, it's marked Leaving Soon once the account that chose it has watched it. **Don't leave after watched** undoes it. Only for people Jellyfin lets delete the item.
- Needs Curatarr 0.2.0rc7 or later for the new item; with older ones the menu is as before.

### 0.5.0.0 (2026-10-25)

- A favourite ribbon can look different on a title's own page, where the poster is shown big: Curatarr draws a second version listing each name, and the plugin serves it when an app asks for a poster at least 300 pixels wide (the Roku app asks for 295 in lists and 300 on a title's page). The width is *LargePosterWidth* in the plugin's settings.
- Needs Curatarr 0.2.0rc7 or later for the second version; with older ones, posters work as before.

### 0.4.0.0 (2026-10-24)

- Each account can have posters of its own. With Curatarr's custom posters for one account, that account sees its poster in every app (web, phone, TV, Roku), and everyone else sees the usual one.
- How: in item details sent to that account, the poster's image tag is swapped for one standing for the account's poster (a show's poster on its episodes' cards too), and an image asked for with that tag is served from Curatarr's picture, resized and cached by Jellyfin like its own. The list is fetched from Curatarr every minute.
- New setting: *Show each account its own posters* (on).
- Needs Curatarr 0.2.0rc6 or later for posters; older Curatarr versions work as before.

### 0.3.0.0 (2026-10-23)

- Each device plays the copy and audio track it can play as it is. When an app asks how to play a title, the plugin asks Jellyfin's own stream builder, with that device's profile, how each copy and each audio track in the same language would play, and points the app at the one needing the least work (played as it is, then copied into another container, then converted).
- A web browser plays the stereo AAC track Curatarr adds instead of having AC3 or E-AC3 converted while it plays; of two copies of a film, each device gets the one it plays directly.
- Commentaries and other languages are never chosen, and a track or copy picked by hand is left alone.
- New setting: *Play the copy and audio track each device can play as it is* (on).

### 0.2.0.0 (2026-10-23)

- Delete on a title that's already Leaving Soon deletes it now, through Radarr or Sonarr, in every app.
- The web app's menu for a film or show Curatarr manages has **Leaving Soon** beside **Delete**, and Delete there deletes straight away. Needs the File Transformation plugin (only the web app's menus can be changed); a setting switches it off.
- Only for people Jellyfin already lets delete the item.

### 0.1.1.0 (2026-10-23)

- Curatarr's logo on the plugin's card in Jellyfin's plugin list.
- Works with Curatarr's one-click **Install in Jellyfin** (Settings), which installs the plugin, restarts Jellyfin and connects the two, with no address or token to type.

### 0.1.0.0 (2026-10-23)

- First release. Delete in any Jellyfin app asks Curatarr first: a film or show Curatarr manages is marked Leaving Soon instead of having its files deleted behind Radarr's and Sonarr's backs, so it leaves through them at the end of the notice period and can still be saved. A title Curatarr must keep (Never Purge) can't be deleted; episodes, music and anything Curatarr doesn't manage are deleted by Jellyfin as usual.
- Settings: Curatarr's address and webhook token, and whether a delete fails while Curatarr can't be reached.

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
