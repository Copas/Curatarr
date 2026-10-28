# 010: Generating artwork through workers, with review before anything changes

Curatarr's posters could already be uploaded, imported from a folder, or made by an AI agent from prompts Curatarr wrote and a person copied across. Generating them from Curatarr itself had to keep everything those offered (accounts, originals restorable, the import naming) and add nothing that changes Jellyfin unasked.

## Workers pull, over HTTP

Image models want a graphics card, which the Curatarr server may not have, and the household's other computer may. So pictures are made by **workers**: a separate program (`curatarr-artwork-worker`) that asks Curatarr for work, makes it and uploads it.

- **Pull, not push.** A worker connects to Curatarr; Curatarr never connects to a worker. No worker port is opened, nothing has to be reachable from the server, and a laptop that sleeps simply stops asking.
- **No shared folders.** Reference photos and the set's poster are fetched per task, by number, with the worker's token; results are uploaded. Paths never leave Curatarr.
- **One protocol for every machine.** The "local" worker is the same program as a remote one, so there's one code path to test.
- **Tokens, hashed.** Each worker has its own random token (`cwk_…`), shown once; Curatarr keeps its SHA-256. Turning a worker off or giving it a new token locks the old one out at once.
- **Leases.** A picture is held for five minutes at a time, renewed by progress reports (and a keep-alive while an engine is quiet). An expired lease puts it back in the queue; three tries by default. A usage limit or a busy graphics card requeues it for later *without* using a try. Everything is in the database, so restarts lose nothing; a worker that can't upload keeps the picture in an outbox.

## Review before Jellyfin

Every picture lands in staging and waits for a person. Approving goes through `custom_posters.apply` / `import_for_accounts`, exactly as an imported file does, so originals are kept, chosen accounts get theirs through the plugin, and Restore works. A set's pictures are decided one by one: three can be approved while two are made again. Batches never replace pictures already chosen.

## Engines

Measured on the development machine (RTX 3080 Ti, 12 GB; 46 GB RAM), with the benchmark in Posters → Benchmarks:

| Engine | Seconds each | Title read back | Likeness | |
|---|---|---|---|---|
| FLUX.2 Klein 4B | 45 | 0.93 | 0.64 | chosen for everything but logos |
| Krea 2 Turbo | 46 | 0.99 | no photos | chosen for logos |
| SDXL + IP-Adapter FaceID | 76 | 0.23 | 0.22 | offered, not recommended |
| FLUX.2 Klein 4B base | ~190 a try | 0.77 | | offered, not recommended |

The details, and what the pictures showed, are in [ARTWORK.md](../ARTWORK.md#results-on-a-12-gb-card). FLUX.2 Klein takes reference photos as reference latents with each person's images numbered in the prompt, which kept two people apart; SDXL's FaceID turns photos into one face embedding, which can't. Neither trains on the photos.

**Codex** is an agent, so it's run with everything but its image tool turned off, a read-only sandbox, an empty folder, no user config or rules, a fixed argument list and the prompt on stdin. Its pictures are collected from where Codex itself saves them. It's an outside service: reference photos go to it only when explicitly allowed per job, and "Best available" never chooses it.

## Prompts for image models

The copy-and-paste prompts are instructions for an agent. Image models read prompts differently, and the first benchmark showed it:

- Listing the year and genres ("(2019), Thriller, Drama") got them lettered onto the poster. The model prompt now names only a genre noun ("a thriller film").
- "Lettering that suits the story" gave misspelled, generic titles. A concrete style by genre ("weathered silver serif capitals") and the title in capitals took the title read-back from about 0.7 to 1.0 on most pictures.
- With one person cast and a story about two characters, the model drew the same person twice. With one person, the prompt now says only the lead appears; the worker also counts faces and retries when someone appears twice.
- Reference images are numbered in the prompt ("Sam (images 1 and 2) and Alex (image 3)"), which FLUX.2 follows, keeping two people apart.

## Checks are signals, not judges

After making a picture the worker reads its title back (RapidOCR) and compares faces with the reference photos (InsightFace buffalo_l, cosine similarity of normalised embeddings). A title read below 0.95, more than 12 letters of stray text, or a person shown twice makes it try again with a new seed (twice by default), keeping the best. Text recognition misreads stylised lettering both ways (a correct "LIGHTHOUSE" read as "LIGHTOUSE"; "THE L LAST" passing at 0.97), so the read text is shown in review and a person decides. Face similarity says nothing about whether a picture is good. Each metric is stored marked *measured* or *estimate*.

## Tried, or considered, and left out

- **Two-pass lettering** (making the art with an empty sky, then an edit pass adding the title): no better at spelling than one pass, at twice the time.
- **FLUX.2 Klein 4B base** (undistilled, 28 steps with guidance): about 18 s a step on 12 GB as ComfyUI streams it from system memory, so 8–9 minutes a try, and no better lettering. Offered, not recommended.
- **Upscaling** (not tried). FLUX.2's native sizes (1024 × 1536, 1920 × 1088) are already at or above the sizes Jellyfin shows posters and backgrounds at, so there was nothing for an upscaler to fix.
- **Face refinement** (a detailer pass on faces; not tried). The faces FLUX.2 made from references scored well and looked right in review; a pass that redraws faces could change a likeness, so it would need its own benchmark first.
- **Training per person** (LoRA): not needed for recognisable likeness with reference latents, and it would mean training on private photos; never done without the owner asking.
- **FLUX.2 Klein 9B** is gated behind a licence acceptance on Hugging Face, so it isn't downloaded automatically; the worker offers it when its files are installed.
