# Generating artwork with AI

Curatarr can make posters, wide tiles, backgrounds, logos and banners itself (**Library → Posters → Generate**), with your review before anything changes in Jellyfin. This page sets up the **worker** that makes them, and explains what each engine does, what leaves your computer, and what to do when something goes wrong. What the pages do is in [FEATURES.md](FEATURES.md#generate-with-ai).

- [How it fits together](#how-it-fits-together)
- [Setting up a worker](#setting-up-a-worker)
- [Engines](#engines)
- [Reference photos](#reference-photos)
- [Benchmarks](#benchmarks)
- [Privacy: what leaves the computer](#privacy-what-leaves-the-computer)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

## How it fits together

```
Curatarr (jobs, review, approval)  ◀── HTTP(S), worker token ──  worker  ──▶  ComfyUI (127.0.0.1)
                                                                        └──▶  Codex CLI
```

- **Curatarr keeps the jobs** in its database: each job is one title's pictures, each picture has its tries, and each try waits in staging (`<instance>/artwork_staging/`) for review.
- **A worker asks for work**, makes it, and sends it back. It only ever connects *to* Curatarr, so it needs no open port, and nothing needs a shared folder: reference photos are fetched from Curatarr for each task with the worker's token, and pictures are uploaded.
- **Approving** a picture puts it on through the same code as the import folder (`custom_posters.apply`, or `import_for_accounts` for chosen accounts), so the original is kept and **Restore original** works as for any custom picture.
- **Leases.** A worker holds a picture for 5 minutes at a time, renewed with each progress report. If a worker stops, crashes or loses the network, the picture goes back in the queue when its lease runs out and is tried again (3 tries by default). Curatarr restarting loses nothing. A picture made while Curatarr was unreachable is kept in the worker's outbox and sent when it's back.
- **Several workers** can share the work: each offers the engines it has, and Curatarr gives each picture to a worker with an engine that can make it (or only the worker a job names). **Pictures made at once** (Workers → Settings) caps it across all of them, and each worker has its own limit.

## Setting up a worker

The worker is part of the Curatarr package (`curatarr-artwork-worker`). On the computer that will make the pictures, which can be the one Curatarr runs on:

1. **Install Curatarr's package** (the worker needs only its Python code, not a running Curatarr):

   ```
   python3 -m venv ~/curatarr-worker
   ~/curatarr-worker/bin/pip install "curatarr[artwork] @ git+https://github.com/Copas/Curatarr@main"
   ```

   On the Curatarr computer itself, the installed Curatarr already has it: use its `curatarr-artwork-worker`.

2. **Add the worker** in Curatarr under **Library → Posters → Workers**. Curatarr shows one command, with the worker's token in it.

3. **Paste that command** into a terminal on the worker's computer. It saves the token (in `~/.config/curatarr-artwork-worker/`, readable only by you), finds Codex and ComfyUI (give `--comfyui /path/to/ComfyUI` if yours is somewhere unusual), and starts the worker as a systemd user service, now and whenever you sign in. It looks like:

   ```
   /opt/curatarr/current/bin/curatarr-artwork-worker setup --server http://192.0.2.10:8787 --token cwk_… --service
   ```

   The token is the worker's own password for talking to Curatarr, which the rest of the network can reach: it's what keeps anything else on the network from fetching your reference photos or sending pictures. Curatarr keeps only a fingerprint of it; **New token** makes another (the old one stops working) with a new command.

   For the checks (text recognition and face recognition, about 600 MB), add `--install-checks` once; without them, pictures aren't checked. `curatarr-artwork-worker check` shows what the worker found, and `journalctl --user -u curatarr-artwork-worker` its log.

### Settings

`config.json` (made by `setup`; edit it to change things):

| Setting | Default | What it does |
|---|---|---|
| `server` | | Curatarr's address |
| `slots` | 1 | pictures this worker makes at once |
| `backends.comfyui.url` | `http://127.0.0.1:8188` | ComfyUI's address |
| `backends.comfyui.folder`, `python` | found by `setup` | where ComfyUI is, so the worker can start it (on 127.0.0.1 only) when there's work |
| `backends.comfyui.stop_after_idle` | 300 | seconds without work before a ComfyUI the worker started is stopped; models are unloaded after 60 |
| `backends.comfyui.min_free_vram_mb` | 6000 | graphics memory a picture needs free; with less (Jellyfin transcoding, another model), it waits and is tried again |
| `backends.codex.enabled` | when Codex is found | offer Codex's image tool |
| `backends.codex.model`, `timeout` | Codex's default, 900 | |
| `checks.python` | the checks environment | a Python with `rapidocr_onnxruntime` and `insightface` |
| `checks.title_retries` | 2 | more tries when the title comes out misspelled (local models only: Codex is asked once, to spare the plan's allowance) |

## Engines

| Engine | Reference photos | Lettering | Notes |
|---|---|---|---|
| **Qwen-Image-Edit 2511** (ComfyUI) | yes, one photo each | good | recommended for pictures with people (see [Benchmarks](#results-on-a-12-gb-card)) |
| FLUX.2 Klein 4B (ComfyUI) | yes, several people | good, not perfect | used when Qwen-Image-Edit isn't installed |
| FLUX.2 Klein 4B base (ComfyUI) | yes | | the undistilled model: many more steps; slower |
| FLUX.2 Klein 9B (ComfyUI) | yes | | offered when its files are installed |
| **Krea 2 Turbo** (ComfyUI) | no | the best measured | recommended for logos; never given jobs with reference photos |
| SDXL + IP-Adapter FaceID (ComfyUI) | one person | poor | two people blend into one face |
| **Codex image tool** | yes | not measured here (its daily allowance was used up) | uses the ChatGPT plan's image allowance; sends prompts and pictures to OpenAI |

**ComfyUI** needs these files in its `models` folder (the worker matches names loosely, so other quantisations count):

- Qwen-Image-Edit 2511: `diffusion_models/qwen_image_edit_2511_fp8_e4m3fn_scaled_lightning_comfyui_4steps_v1.0.safetensors` (from [lightx2v/Qwen-Image-Edit-2511-Lightning](https://huggingface.co/lightx2v/Qwen-Image-Edit-2511-Lightning), 20 GB), `text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` (from [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI), 9 GB), `vae/qwen_image_vae.safetensors`. Apache 2.0; it streams from system memory on a 12 GB card, so allow 40 GB of RAM.
- FLUX.2 Klein 4B: `diffusion_models/flux-2-klein-4b-fp8.safetensors`, `text_encoders/qwen_3_4b.safetensors`, `vae/flux2-vae.safetensors`.
- Krea 2 Turbo: `diffusion_models/krea2_turbo_fp8_scaled.safetensors`, `text_encoders/qwen3vl_4b_fp8_scaled.safetensors`, `vae/qwen_image_vae.safetensors`.
- SDXL + FaceID: an SDXL checkpoint (`checkpoints/Juggernaut-XL…`), the [ComfyUI_IPAdapter_plus](https://github.com/cubiq/ComfyUI_IPAdapter_plus) nodes, `ipadapter/ip-adapter-faceid-plusv2_sdxl.bin`, its LoRA, and InsightFace.

Check each model's licence before use; FLUX.2 Klein's and InsightFace's model weights carry their own terms.

**Codex** needs the Codex CLI installed and signed in on the worker's computer (`codex login`; the worker reads `codex login status`, which never shows the token, and Curatarr never sees it). The worker checks that Codex has its `image_generation` feature. It runs Codex with a fixed argument list (never through a shell), the prompt on stdin, its shell, browser, apps, plugins and hooks turned off, a read-only sandbox, an empty working folder and without the user's Codex settings, and collects the picture Codex's image tool saves under `~/.codex/generated_images/`. When the plan's image allowance is used up, the worker reads when it resets and the picture waits until then without using up a try.

### How pictures are made

- **Sizes.** FLUX.2 makes a poster at 1024 × 1536 and a wide tile or background at 1920 × 1088 (cropped to 1920 × 1080), a banner at 1728 × 320; SDXL a little under half that. Curatarr's own limits (`normalise`) apply when it goes on.
- **Sets.** A job's poster is made first; its wide tile, background and banner are given that poster as a reference, so the cast and look match, in their own scenes (they aren't crops of it).
- **Logos.** The image models can't make transparency, so a logo is made on plain black and the background is keyed out (soft edges, the black taken out of half-transparent pixels); if that leaves too much or too little, it's made again on white. Curatarr refuses a logo without real transparency.
- **Lettering.** The prompts ask for the title in the film's own style, made as part of the picture, never typed on. Image models still misspell sometimes: the worker reads the title back and makes it again (up to twice more) when it's wrong, keeping the best; the review shows what was read. With **Use each title's original art** on, the title's original art is given as a reference: Codex gets its original poster, logo and picture of the type it's making (for the feel and lettering); a local model gets its logo (for the lettering). It's read from beside the title's files when Jellyfin saves art there, else from Jellyfin (the picture from before any custom one).

## Reference photos

Put photos in a folder, each file named after who's in it (`Sam 1.jpg`, `Sam beach.png`, `Alex.jpg`), and give the folder on the Generate page; **Who's in them** limits it to some of them (up to three). A text file per person, named after them (`Sam.txt`), with a few words on them ("a woman of about 30, freckles, short platinum-blonde pixie cut") goes into the prompt beside their photos: it stops a model changing someone's sex or age in a busy picture. Use several photos of each person, different angles and lighting: each picture uses two of them (a different pair leads each time), plus the set's poster. Photos with the face clear and large work best. Only use photos of people who've agreed.

Where a person's face is small in a photo (under a fifth of its width), the worker crops it to head and shoulders before the model sees it, so the face isn't squeezed to a few dozen pixels.

**How likeness is kept.** FLUX.2 Klein takes each photo as an image input (a reference latent) and the prompt says which images are whom ("Sam (images 1 and 2) and Alex (image 3)"), so two people stay two people. SDXL's FaceID turns one person's photos into a face embedding: it can't keep two people apart. No model is trained on your photos.

**How likeness is checked.** Face recognition (InsightFace) compares every face in a picture with each person's photos. **Likeness** is the similarity of the least-like person's best face: above about 0.45 is usually the same person, and 0.6 is very close. **Identity mix** says whether one face was taken for two people. It's an estimate: a good picture can score lower (a face in profile, heavy make-up, a painted style), and it says nothing about whether the picture's any good. The review shows the photos next to the picture, to judge for yourself.

## Benchmarks

**Posters → Benchmarks** asks each engine for the same made-up films and records, for every picture: the engine and its settings, the seed, the time, the graphics card's peak memory, the checks (title read back, stray text, likeness, identity mix, sharpness, shape) and your ratings. *Compare blind* shows two engines' pictures of the same scenario in random order; which was which is shown after you choose. **Set as default** makes an engine the one used for a picture type when a job leaves it to Curatarr.

Reference photos: the worker can make photos of two made-up people to benchmark with, so results can be shared:

```
curatarr-artwork-worker make-test-people ~/artwork-test-people
```

For a **private evaluation** with your own photos, give your photos folder instead and choose local engines only: the photos stay on your computers.

### Results on a 12 GB card

Measured on an RTX 3080 Ti (12 GB) with 46 GB of memory, ComfyUI 0.33, the worker's checks on, two tries more when a check fails; made-up people from `make-test-people`; the full run (11 scenarios) and two follow-up runs after the prompt changes. Times are per finished picture, tries and checks included. The graphics card's peak was 11.5 GB for every engine: ComfyUI uses what's free and streams the rest from memory.

| Engine | Pictures | Seconds each | Title read back | Likeness | Verdict |
|---|---|---|---|---|---|
| FLUX.2 Klein 4B | 20 of 20 | 45 (10–166) | 0.93 | 0.64 (0.34–0.74) | **Recommended** for posters, wide tiles, backgrounds and banners: recognisable people, two people kept apart, matching sets |
| Krea 2 Turbo | 6 of 6 | 46 | 0.99 | (no photos) | **Recommended for logos**; the best lettering, long titles included, but it can't use reference photos or match a set |
| SDXL + IP-Adapter FaceID | 20 of 20 | 76 | 0.23 | 0.22 | Not recommended: no usable lettering, and two people became one face (or a grid of the same face) |
| FLUX.2 Klein 4B base | 1 | about 190 a try (18 s a step) | 0.77 | | Not recommended on 12 GB: much slower, no better |

What the pictures showed, beyond the numbers:

- **Real people are harder than made-up ones.** With a household's own photos, FLUX.2 Klein 4B's likeness was 0.2–0.5 (around 0.45 is usually "the same person"), against 0.6–0.7 for the made-up people, which FLUX.2 had made itself. More reference photos per person didn't help; cropping small faces to head and shoulders helped a little.
- **People.** With FLUX.2 Klein, the made-up man and woman were recognisable in every poster and background, and two people stayed two different people. In a busy set, the woman was once drawn as a white-haired man, though face similarity still scored her 0.53; a one-line description beside the photos ([`Sam.txt`](#reference-photos)) fixed it (0.70–0.74 across the set).
- **Lettering.** Once the prompt gave the title in capitals with a concrete style, FLUX.2's posters read back exactly on most tries; long titles and wide tiles are its weak spots (a wide tile's title was still wrong after three tries). Krea 2 spelled even "The Quixotic Adventures of Zephyr Blackwood" right.
- **Stray text.** Both FLUX.2 and Krea 2 sometimes add small gibberish credits, and text on backgrounds; the checks count it and try again, but a busy street's signs are sometimes real.
- **Transparent logos** keyed cleanly from black or white backgrounds, at both engines.

**With a household's own photos** (the second round of testing, posters for real favourite films): FLUX.2 Klein 4B kept likeness at 0.15–0.45 and lost each film's look (two people in leather jackets for *Batman Returns*). Qwen-Image-Edit 2511, given one head-cropped photo per person, reached 0.30–0.66 (most at 0.45 or above), put the cast in the film's own costumes and settings (Batman suits, sombreros, a vampire's cloak) and spelled most titles right, at 2–3 minutes a picture with its checks and retries. What got it there, each measured:

- **One photo each, not several side by side** (the harder-to-match person scored 0.16–0.24 side by side, 0.45 with one).
- **Crop every photo to the head**: shown a whole photo, the model copied its clothes and the writing on them.
- **The most typical photo**, compared with the person's others, so a photo in sunglasses isn't the one used (it was copied onto every picture).
- **"They play the story's own characters, in their costumes"** in the prompt.
- Not the film's original poster as a mood reference: it brought in the actors' faces (likeness 0.02).

When a job leaves the engine to Curatarr and no default is set under Workers, it uses Krea 2 Turbo for logos and banners and Qwen-Image-Edit (or, without it, FLUX.2 Klein) for everything else (FLUX.2 Klein 9B first, when installed). FLUX.2 Klein 9B (a gated download; accept its licence on Hugging Face, then put its files in ComfyUI's `models`) and Codex wasn't benchmarked (its allowance is the plan's), but a household compared ChatGPT's pictures with the local ones on their own titles and preferred ChatGPT's every time; a test poster through Curatarr's Codex worker took 2 minutes 20 seconds and scored 0.59 for likeness.

## Privacy: what leaves the computer

| | ComfyUI engines | Codex |
|---|---|---|
| Prompt (title, year, genres, plot, the look) | stays on the worker | sent to OpenAI |
| Reference photos of people | fetched by the worker from Curatarr; stay on it | sent to OpenAI only when **Allow the reference photos to be sent to an outside service** is ticked |
| Original art, the set's poster | stay on the worker | sent to OpenAI |
| Curatarr ↔ worker | your network, with the worker's token; use HTTPS across untrusted networks | |

A job left to Curatarr ("Best available") never picks an outside engine. Curatarr serves a task's references only to the worker holding that task, never sends paths, and keeps only a hash of each worker's token. ComfyUI is started listening on 127.0.0.1 only.

## Troubleshooting

- **A worker shows "Not connected yet" or "Last seen …".** Run `curatarr-artwork-worker check` on it: it shows whether Curatarr's reachable and what it can make. A refused token means the worker was turned off or given a new token: run `setup` again with the new one.
- **Pictures stay "Waiting".** No online worker has an engine that can make them: a job with reference photos needs an engine that takes them (not Krea 2), a job that names a worker waits for that worker, and pictures wait while Curatarr's work is paused. The picture's message says why when a worker tried (*The graphics card is busy…*, *the image allowance is used up…*).
- **"The graphics card is busy".** Something else holds its memory (Jellyfin transcoding, Ollama, a game). The picture is tried again every few minutes without using up a try. Lower `min_free_vram_mb` only if the model really fits in less.
- **A picture failed.** Its page shows the error from each try. **Try the failed ones again** (on the job, or **Retry failed** on a batch) starts it over. The worker's log is where `run` was started, or `journalctl --user -u curatarr-artwork-worker`; ComfyUI's is `~/.cache/curatarr-artwork-worker/comfyui.log`.
- **Interrupted jobs** need nothing: a picture whose worker stopped goes back in the queue when its lease runs out (about 5 minutes) and is tried again.
- **A misspelled title got through.** All tries misspelled it; the review shows what was read. Make it again with feedback ("the title must read …"), or use Codex for that picture.

## Limitations

- Image models still misspell titles, especially long ones and small lettering on wide tiles. The checks catch most and retry, but read every title before approving.
- Face recognition measures similarity, not quality: a high score doesn't make a good picture, and a stylised picture can score low while looking right.
- Logos are keyed from a plain background: very thin or very dark lettering can lose detail.
- SDXL with FaceID keeps only one person's likeness and can't letter titles reliably.
- On a 12 GB card, one picture is made at a time; the local models share the card with Jellyfin's transcoding and any language model, and wait for them.
- Codex's image allowance is the ChatGPT plan's, shared with anything else made with it; when it's used up, Codex pictures wait for the daily reset.
