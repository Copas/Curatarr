"""Chapter enrichment: post-import jobs that add chapters to files without them.

This is maintenance, never acquisition. The worker queues files Sonarr/Radarr
finished importing (or files an admin selects), and runs each job in a
background thread so a long transcription never delays the lifecycle cycle.
Database leases cap concurrent jobs across processes.

Order of preference for a file with no chapters: Smart (transcript + Ollama),
then scene-aligned, then fixed intervals. Existing chapters are left alone
unless the library opts out of preserving them, and Curatarr only removes or
regenerates chapters it can show it wrote (see ChapterState).
"""

import json
import logging
import os
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import current_app
from sqlalchemy import and_, case, func, or_, update
from sqlalchemy.exc import OperationalError

from . import db, leases, throttle
from .chapters import (
    DEFAULTS,
    GENERATED_STATUSES,
    GENERATION_VERSION,
    GLOBAL_DEFAULTS,
    Chapter,
    ChapterConfigError,
    InvalidChapters,
    chapter_file_text,
    config_hash,
    effective_config,
    fingerprint,
    fixed_interval_plan,
    scene_aligned_plan,
    semantic_plan,
    timestamp,
    validate_chapters,
    validate_global_options,
    validate_layer,
)
from .integrations import IntegrationError
from .mediafile import (
    MediaInfo,
    MediaToolError,
    can_write,
    detect_scenes,
    export_chapters_xml,
    probe,
    remove_chapters,
    remove_command,
    restore_chapters_xml,
    write_chapters,
    write_command,
)
from .models import ChapterState, Library, MediaIdentity, MediaPart, utcnow
from .semantic import LanguageModelUnavailable
from .services import _as_datetime, audit, client, set_setting, setting
from .subtitles import (
    cues_from,
    digest,
    embedded_english,
    generated_path,
    sidecar_english,
    srt_text,
)

log = logging.getLogger("curatarr.chapters")

WRITE_ACTIONS = {"auto", "generate", "regenerate", "remove"}
SUBTITLE_ACTIONS = {"subtitles", "remove_subtitles"}
ACTIONS = WRITE_ACTIONS | SUBTITLE_ACTIONS | {"preview", "scan", "recap"}
FINAL_STATUSES = {
    "existing",
    "generated_semantic",
    "generated_scene_aligned",
    "generated_fixed",
    "skipped_short",
    "unsupported_container",
    "dry_run",
    "removed",
}
MAX_ATTEMPTS = 3
RETRY_MINUTES = 30
DEFER_MINUTES = 15
LEASE_SECONDS = 600
RECAP_BATCH = 100  # recap checks run back to back in one slot, like scans
SCAN_BATCH = 200
IMPORT_COMMANDS = {
    "DownloadedEpisodesScan",
    "DownloadedMoviesScan",
    "ProcessMonitoredDownloads",
    "ManualImport",
    "RenameFiles",
    "RenameSeries",
    "RenameMovie",
    "MoveSeries",
    "MoveMovie",
    "RescanSeries",
    "RescanMovie",
}


# --- Settings ---------------------------------------------------------------


def global_layer() -> dict:
    return setting("chapter_settings", {}) or {}


def library_layers() -> dict:
    return setting("chapter_library_settings", {}) or {}


def options() -> dict:
    return validate_global_options(setting("chapter_options", {}) or {})


def config_for(library_id: str) -> dict:
    return effective_config(global_layer(), library_layers().get(library_id))


def _record_enabled_since():
    """When each library's chapter generation was switched on.

    Automatic processing of new media starts from this moment, so enabling the
    feature never sweeps the existing library.
    """
    since = dict(setting("chapter_enabled_since", {}) or {})
    now = utcnow().isoformat()
    changed = False
    for library in db.session.query(Library).all():
        if not library.managed:
            continue
        enabled = config_for(library.id)["enabled"]
        if enabled and library.id not in since:
            since[library.id] = now
            changed = True
        elif not enabled and library.id in since:
            del since[library.id]
            changed = True
    if changed:
        set_setting("chapter_enabled_since", since)


FIELD_PATHS = {
    "minimum_duration_minutes": ("minimum_duration_minutes",),
    "target_spacing_minutes": ("target_spacing_minutes",),
    "minimum_spacing_minutes": ("minimum_spacing_minutes",),
    "maximum_spacing_minutes": ("maximum_spacing_minutes",),
    "preserve_existing": ("preserve_existing",),
    "scene_detection": ("fallback", "scene_detection"),
    "fixed_intervals": ("fallback", "fixed_intervals"),
    "semantic_enabled": ("semantic", "enabled"),
    "whisper_model": ("semantic", "model"),
    "llm_model": ("semantic", "llm_model"),
    "semantic_names": ("naming", "semantic_names"),
    "fallback_format": ("naming", "fallback_format"),
    "new_media": ("processing", "new_media"),
    "existing_library": ("processing", "existing_library"),
    "dry_run": ("processing", "dry_run"),
    "subtitles_enabled": ("subtitles", "enabled"),
}


def _template_value(path):
    value = DEFAULTS
    for key in path:
        value = value[key]
    return value


def _assign(layer, path, value):
    target = layer
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value


def parse_layer(form, *, library: bool) -> dict:
    """Form fields to a stored layer; blank/inherit leaves a library field unset."""
    layer: dict = {}
    mode = (form.get("chapter_mode") or "").strip()
    if mode == "disabled":
        layer["enabled"] = False
    elif mode in {"navigation", "smart"}:
        layer["enabled"] = True
        layer["mode"] = mode
    elif mode not in {"", "inherit"} or not library:
        raise ChapterConfigError("Choose a chapter mode")
    for field_name, path in FIELD_PATHS.items():
        expected = _template_value(path)
        raw = form.get(field_name)
        if isinstance(expected, bool):
            if library:
                raw = (raw or "").strip()
                if raw in {"", "inherit"}:
                    continue
                if raw not in {"true", "false"}:
                    raise ChapterConfigError(f"Invalid {field_name}")
                _assign(layer, path, raw == "true")
            else:
                # Global checkboxes: absent means off.
                _assign(layer, path, raw in {"true", "on", "1"})
            continue
        raw = (raw or "").strip()
        if raw in {"", "inherit"}:
            if library or field_name == "llm_model":
                continue
            raise ChapterConfigError(f"{field_name.replace('_', ' ')} is required")
        if isinstance(expected, int):
            try:
                _assign(layer, path, int(raw))
            except ValueError as exc:
                raise ChapterConfigError(f"Invalid {field_name}") from exc
        else:
            _assign(layer, path, raw)
    return validate_layer(layer)


def parse_options(form) -> dict:
    values = {}
    for key in ("concurrency", "settle_minutes"):
        raw = (form.get(key) or "").strip()
        if raw:
            try:
                values[key] = int(raw)
            except ValueError as exc:
                raise ChapterConfigError(f"Invalid {key}") from exc
    raw = (form.get("scene_threshold") or "").strip()
    if raw:
        try:
            values["scene_threshold"] = float(raw)
        except ValueError as exc:
            raise ChapterConfigError("Invalid scene threshold") from exc
    for key in ("ollama_url", "whisper_device"):
        raw = (form.get(key) or "").strip()
        if raw:
            values[key] = raw
    # A checkbox sends nothing when unticked, so only a form that showed it
    # (marked by the hidden field) may turn it off. A page loaded before the
    # option existed would otherwise switch it off when saved.
    for key in (
        "pause_during_playback",
        "movie_credits_only",
        "episode_identity_check",
        "dub_check",
        "dub_replace_automatically",
        "dub_remove_other_languages",
    ):
        if f"{key}_shown" in form:
            values[key] = form.get(key) in {"true", "on", "1"}
        else:
            values[key] = options()[key]
    mappings = []
    for line in (form.get("path_mappings") or "").splitlines():
        if not line.strip():
            continue
        if "=>" not in line:
            raise ChapterConfigError('Write path mappings as "arr path => local path"')
        arr, local = (part.strip() for part in line.split("=>", 1))
        mappings.append({"arr": arr, "local": local})
    values["path_mappings"] = mappings
    return validate_global_options(values)


def save_settings(library, form):
    if library is None:
        layer = parse_layer(form, library=False)
        new_options = parse_options(form)
        # Validate every library against the new global layer before saving.
        for library_layer in library_layers().values():
            effective_config(layer, library_layer)
        set_setting("chapter_settings", layer)
        set_setting("chapter_options", new_options)
    else:
        layer = parse_layer(form, library=True)
        effective_config(global_layer(), layer)
        layers = dict(library_layers())
        if layer:
            layers[library.id] = layer
        else:
            layers.pop(library.id, None)
        set_setting("chapter_library_settings", layers)
    _record_enabled_since()


def flatten(config: dict) -> dict:
    """A (possibly partial) layer keyed by form field name."""
    values = {}
    for field_name, path in FIELD_PATHS.items():
        value = config
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        values[field_name] = value
    return values


def form_values(library=None) -> dict:
    """Current values for the settings page, flattened to form field names."""
    layer = library_layers().get(library.id, {}) if library else global_layer()
    shown = layer if library else effective_config(layer)
    values = flatten(shown)
    if library:
        values["chapter_mode"] = (
            None
            if "enabled" not in layer and "mode" not in layer
            else ("disabled" if layer.get("enabled") is False else layer.get("mode"))
        )
    else:
        values["chapter_mode"] = shown["mode"] if shown["enabled"] else "disabled"
    return values


# --- Paths and safety checks ------------------------------------------------


def local_path(arr_path: str | None, mappings=None) -> str | None:
    """Translate an arr-side path to this host's path (longest prefix wins)."""
    if not arr_path:
        return None
    mappings = options()["path_mappings"] if mappings is None else mappings
    best = None
    for mapping in mappings:
        prefix = mapping["arr"].rstrip("/\\")
        matches = arr_path == prefix or arr_path.startswith(
            (prefix + "/", prefix + "\\")
        )
        if matches and (best is None or len(prefix) > len(best["arr"].rstrip("/\\"))):
            best = mapping
    if not best:
        return arr_path
    rest = arr_path[len(best["arr"].rstrip("/\\")) :].replace("\\", "/")
    return best["local"].rstrip("/") + rest


def jellyfin_item_id(part: MediaPart, media: MediaIdentity) -> str | None:
    if part.jellyfin_id:
        return part.jellyfin_id
    return media.jellyfin_id if part.kind == "movie_file" else None


def _records(payload):
    if isinstance(payload, dict):
        return payload.get("records") or []
    return payload or []


def import_activity(part: MediaPart, media: MediaIdentity, info: MediaInfo, settle):
    """Why the file might still be changing, or None when it is safe to edit.

    Fails closed: if Sonarr/Radarr/Jellyfin cannot be asked, the file waits.
    """
    age = time.time() - info.mtime
    if age < settle * 60:
        return (
            f"The file changed {round(age / 60)} minutes ago; waiting for it to settle"
        )
    try:
        if media.media_type == "series":
            arr, key, ident = client("sonarr"), "seriesId", media.sonarr_id
        else:
            arr, key, ident = client("radarr"), "movieId", media.radarr_id
        for record in _records(arr.queue()):
            if record.get(key) != ident:
                continue
            episode = record.get("episodeId")
            if (
                media.media_type == "series"
                and episode
                and (episode != part.sonarr_episode_id)
            ):
                continue
            return "A download for this title is still in the Sonarr/Radarr queue"
        for command in arr.commands() or []:
            if command.get("status") not in {"started", "queued"}:
                continue
            if command.get("name") not in IMPORT_COMMANDS:
                continue
            body = command.get("body") or {}
            targets = (
                {body.get("seriesId"), body.get("movieId")}
                | set(body.get("seriesIds") or [])
                | set(body.get("movieIds") or [])
            )
            targets.discard(None)
            if not targets or ident in targets:
                return f"Sonarr/Radarr is running {command.get('name')}"
        item_id = jellyfin_item_id(part, media)
        if item_id:
            for session in client("jellyfin").sessions() or []:
                if (session.get("NowPlayingItem") or {}).get("Id") == item_id:
                    return "Someone is playing this file in Jellyfin"
    except IntegrationError as exc:
        return f"Could not confirm the import is finished ({exc})"
    return None


# --- Analysis ---------------------------------------------------------------


def cache_dir() -> Path:
    path = Path(current_app.instance_path) / "chapters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _scene_cache_path(info: MediaInfo, threshold: float) -> Path:
    import hashlib

    key = hashlib.sha256(
        f"{info.path}|{info.size}|{int(info.mtime)}|{threshold}".encode()
    ).hexdigest()[:32]
    return cache_dir() / "scenes" / f"{key}.json"


def scene_changes(info: MediaInfo, threshold: float):
    path = _scene_cache_path(info, threshold)
    if path.exists():
        try:
            return [tuple(row) for row in json.loads(path.read_text())], True
        except (OSError, ValueError):
            pass
    scenes = detect_scenes(info.path, threshold)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scenes))
    return scenes, False


def _language_model(config, opts):
    from .semantic import Ollama

    return Ollama(opts["ollama_url"], config["semantic"]["llm_model"])


def analyse(
    info: MediaInfo,
    config: dict,
    opts: dict,
    *,
    every_method=False,
    llm=None,
    context="",
    find_recap=None,
):
    """Plan chapters, falling back Smart → scene-aligned → fixed intervals.

    ``find_recap(segments, scenes, model)`` returns a recaps.Recap or None; a
    recap becomes a "Previously On" first chapter in the Smart plan.

    With ``every_method`` all three are attempted for a preview. Returns a
    dict with the chosen plan, every attempt, and the scene/transcript data.
    """
    started = time.monotonic()
    result = {
        "duration": info.duration,
        "scenes": None,
        "scene_error": None,
        "transcript": None,
        "attempts": [],
        "plans": {},
        "chosen": None,
        "recap": None,
    }
    smart = config["mode"] == "smart" and config["semantic"]["enabled"]
    scenes = None
    if config["fallback"]["scene_detection"] or smart or every_method:
        try:
            raw, was_cached = scene_changes(info, opts["scene_threshold"])
            scenes = [time_ for time_, _score in raw]
            result["scenes"] = {
                "count": len(raw),
                "cached": was_cached,
                "threshold": opts["scene_threshold"],
                "changes": [[t, s] for t, s in raw],
            }
        except MediaToolError as exc:
            result["scene_error"] = str(exc)

    def attempt(method, build):
        try:
            plan = build()
            validate_chapters(plan.chapters, info.duration)
        except (InvalidChapters, MediaToolError, RuntimeError, ValueError) as exc:
            result["attempts"].append(
                {"method": method, "ok": False, "error": str(exc)}
            )
            if isinstance(exc, LanguageModelUnavailable):
                result["language_model_unavailable"] = True
            return None
        result["attempts"].append({"method": method, "ok": True})
        result["plans"][method] = plan.as_dict()
        if result["chosen"] is None:
            result["chosen"] = method
        return plan

    if smart or (every_method and config["semantic"]["enabled"]):

        def smart_plan():
            from .semantic import choose_boundary, name_chapters
            from .transcribe import transcribe

            model = llm or _language_model(config, opts)
            segments, details = transcribe(
                info,
                config["semantic"]["model"],
                opts["whisper_device"],
                cache_dir() / "transcripts",
                make_room=getattr(model, "unload", None),
            )
            from .transcribe import cache_key

            result["transcript"] = {
                **details,
                "key": cache_key(info, config["semantic"]["model"]),
                "segments": len(segments),
                "words": sum(len(s.text.split()) for s in segments),
                "sample": [[round(s.start, 1), s.text.strip()] for s in segments[:12]],
            }
            recap = None
            if find_recap is not None:
                recap = find_recap(segments, scenes or [], model)
                result["recap"] = recap.as_dict() if recap else None
            plan = semantic_plan(
                info.duration,
                scenes or [],
                segments,
                config,
                lambda window, candidates, excerpt: choose_boundary(
                    model, window, candidates, excerpt
                ),
                lambda excerpts: name_chapters(model, excerpts, context),
                recap=recap,
            )
            plan.notes.append(f"{model.calls} Ollama requests to {model.model}")
            return plan

        attempt("semantic", smart_plan)
    if config["fallback"]["scene_detection"] or every_method:
        if scenes is not None:
            attempt(
                "scene_aligned",
                lambda: scene_aligned_plan(info.duration, scenes, config),
            )
        else:
            result["attempts"].append(
                {
                    "method": "scene_aligned",
                    "ok": False,
                    "error": result["scene_error"] or "Scene detection did not run",
                }
            )
    if every_method or (
        config["fallback"]["fixed_intervals"] and result["chosen"] is None
    ):
        attempt("fixed", lambda: fixed_interval_plan(info.duration, config))
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    return result


def file_exists(path) -> bool:
    return os.path.isfile(path)


def file_identity(path) -> tuple[int, float]:
    stat = os.stat(path)
    return stat.st_size, stat.st_mtime


def _plan_chapters(plan_dict) -> list[Chapter]:
    return [Chapter(c["start"], c["name"]) for c in plan_dict["chapters"]]


# --- Jobs -------------------------------------------------------------------


def state_for(part: MediaPart) -> ChapterState:
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).first()
    if not state:
        state = ChapterState(
            media_part_id=part.id,
            media_identity_id=part.media_identity_id,
            chapter_status="not_processed",
            job_state="idle",
            attempts=0,
        )
        db.session.add(state)
        db.session.flush()
    return state


def request(part: MediaPart, action: str) -> ChapterState:
    """Queue one action for one file (deduplicated: a queued job is replaced)."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown chapter action: {action}")
    state = state_for(part)
    if state.job_state == "running":
        raise ValueError("A chapter job is already running for this file")
    state.job_state = "queued"
    state.requested_action = action
    state.requested_at = utcnow()
    state.next_attempt_at = None
    if action != "auto":
        state.attempts = 0
    if action == "recap":
        # Asked for from the episode's page: redo everything, ahead of any
        # library-wide backlog of checks.
        state.recap_checked_at = state.preview_checked_at = None
        state.markers_checked_at = None
        state.requested_at = utcnow() - timedelta(days=3650)
    db.session.commit()
    return state


def queue_new_media() -> int:
    """Queue files imported since chapters were enabled for their library.

    A file already handled is queued again only when Sonarr/Radarr report a
    newer import of it (an upgrade replaces the file and its date added).
    """
    since = setting("chapter_enabled_since", {}) or {}
    queued = 0
    now = utcnow()
    for library in db.session.query(Library).all():
        if not library.managed:
            continue
        try:
            config = config_for(library.id)
        except ChapterConfigError:
            continue
        if not config["enabled"]:
            continue
        processing = config["processing"]
        if not (processing["new_media"] or processing["existing_library"]):
            continue
        start = _as_datetime(since.get(library.id))
        query = (
            db.session.query(MediaPart)
            .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(
                MediaIdentity.library_id == library.id,
                MediaIdentity.missing_since.is_(None),
                MediaPart.has_file.is_(True),
                MediaPart.file_path.isnot(None),
            )
        )
        if not processing["existing_library"]:
            if not start:
                continue
            query = query.filter(MediaPart.acquired_at >= start)
        states = {
            row.media_part_id: row
            for row in db.session.query(ChapterState)
            .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
            .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(MediaIdentity.library_id == library.id)
        }
        for part in query:
            state = states.get(part.id)
            if state is None:
                state = state_for(part)
            elif not _due_automatically(state, part, now):
                continue
            state.job_state = "queued"
            state.requested_action = "auto"
            state.requested_at = now
            queued += 1
    db.session.commit()
    return queued


def _due_automatically(state, part, now) -> bool:
    if state.job_state != "idle":
        return False
    processed = _as_datetime(state.chapter_processed_at)
    acquired = _as_datetime(part.acquired_at)
    if processed and acquired and acquired > processed:
        return True  # re-imported (an upgrade) since Curatarr handled it
    if state.chapter_status == "failed":
        retry_at = _as_datetime(state.next_attempt_at)
        return state.attempts < MAX_ATTEMPTS and (not retry_at or retry_at <= now)
    # Never handled: new, or only inspected by a library scan.
    return state.chapter_status == "not_processed" and processed is None


def _finish(state, status, *, error=None, info=None, source=None, count=None):
    state.chapter_status = status
    state.chapter_error = error
    if source is not None:
        state.chapter_source = source
    if count is not None:
        state.chapter_count = count
    if info is not None:
        state.chapter_file_size = info.size
        state.chapter_file_mtime = info.mtime
        state.duration_seconds = info.duration
        state.container = info.format_name[:60]
    state.chapter_processed_at = utcnow()
    state.job_state = "idle"
    state.requested_action = None
    state.next_attempt_at = None


TRANSCRIBER_MISSING = (
    "Smart Chapters is installed without faster-whisper, so it can't transcribe. "
    "Reinstall Curatarr with EXTRAS=chapters-gpu"
)


def smart_chapters_blocked() -> bool:
    """True when some library uses Smart Chapters but this install can't transcribe."""
    from .transcribe import available

    return bool(_smart_library_ids()) and not available()


def _transcriber_missing(info, config) -> bool:
    from .transcribe import available, cached

    if not (config["mode"] == "smart" and config["semantic"]["enabled"]) or available():
        return False
    return (
        cached(info, config["semantic"]["model"], cache_dir() / "transcripts") is None
    )


def _defer(state, reason):
    state.job_state = "queued"
    state.next_attempt_at = utcnow() + timedelta(minutes=DEFER_MINUTES)
    state.chapter_error = f"Waiting: {reason}"


def _fail(state, message, *, info=None, retry=True):
    _finish(state, "failed", error=message, info=info)
    state.attempts += 1
    if retry and state.attempts < MAX_ATTEMPTS:
        state.next_attempt_at = utcnow() + timedelta(minutes=RETRY_MINUTES)


def _log(event, **fields):
    log.info(json.dumps({"event": event, **fields}, default=str, sort_keys=True))


def curatarr_wrote(state: ChapterState, info: MediaInfo) -> bool:
    """True only while the file still carries exactly the chapters Curatarr wrote."""
    return bool(
        state.written_fingerprint
        and info.chapters
        and fingerprint(info.chapters) == state.written_fingerprint
    )


def write_operation(info: MediaInfo, chapters: list[Chapter]) -> dict:
    """The exact edit a write would make, for previews and history."""
    return {
        "tool": "mkvpropedit",
        "command": write_command(info.path, "<generated-chapters.txt>"),
        "chapter_file": chapter_file_text(chapters),
        "in_place": True,
        "reencode": False,
    }


def run_job(state_id: str, *, llm=None) -> str:
    """Run the queued action for one file; returns the resulting status."""
    state = db.session.get(ChapterState, state_id)
    if not state or state.job_state != "running":
        return "skipped"
    action = state.requested_action or "auto"
    part = db.session.get(MediaPart, state.media_part_id)
    media = db.session.get(MediaIdentity, state.media_identity_id)
    started = time.monotonic()
    path = local_path(part.file_path) if part else None
    fields = {"state_id": state.id, "action": action, "file": path}
    if not part or not media:
        _fail(state, "The media item no longer exists", retry=False)
        db.session.commit()
        return state.chapter_status
    try:
        config = config_for(media.library_id)
        opts = options()
    except ChapterConfigError as exc:
        _fail(state, f"Invalid chapter settings: {exc}", retry=False)
        db.session.commit()
        return state.chapter_status
    if action in SUBTITLE_ACTIONS:
        return run_subtitles(state, action, part, media, path, config, opts)
    if not path or not file_exists(path):
        _fail(state, "The media file was not found at its expected path", retry=False)
        db.session.commit()
        _log("chapters_failed", error=state.chapter_error, **fields)
        return state.chapter_status
    try:
        info = probe(path)
    except (MediaToolError, OSError) as exc:
        _fail(state, str(exc))
        db.session.commit()
        _log("chapters_failed", error=str(exc), **fields)
        return state.chapter_status
    state.duration_seconds = info.duration
    state.container = info.format_name[:60]
    fields |= {"duration": info.duration, "existing_chapters": len(info.chapters)}

    if action == "scan":
        return _scan_result(state, info, config)
    if action == "recap":
        return run_recap(state, part, media, info, config, opts, fields, llm=llm)

    ours = curatarr_wrote(state, info)
    # Unchanged since Curatarr last handled it: nothing to do automatically.
    if (
        action == "auto"
        and state.chapter_status in FINAL_STATUSES
        and state.chapter_file_size == info.size
        and state.chapter_file_mtime == info.mtime
    ):
        state.job_state, state.requested_action = "idle", None
        db.session.commit()
        return state.chapter_status

    if action == "remove":
        return _remove(state, part, media, info, ours, fields, started)

    if info.chapters and not ours:
        if action == "regenerate":
            _fail(
                state,
                "These chapters were not written by Curatarr, so they are left alone",
                info=info,
                retry=False,
            )
            db.session.commit()
            return state.chapter_status
        if config["preserve_existing"] and action != "preview":
            _finish(
                state,
                "existing",
                info=info,
                source="embedded",
                count=len(info.chapters),
            )
            db.session.commit()
            _log("chapters_preserved", **fields)
            return state.chapter_status
    if info.chapters and ours and action in {"auto", "generate"}:
        _finish(state, state.chapter_status, info=info)
        db.session.commit()
        return state.chapter_status

    if info.duration < config["minimum_duration_minutes"] * 60:
        if action == "preview":
            state.last_preview_json = _preview_record(info, None, config, ours)
            state.job_state, state.requested_action = "idle", None
        else:
            _finish(state, "skipped_short", info=info)
        db.session.commit()
        _log("chapters_skipped_short", **fields)
        return state.chapter_status

    writable, reason = can_write(info)
    if not writable and action in WRITE_ACTIONS:
        if "permission" in (reason or ""):
            _fail(state, reason, info=info, retry=False)
        else:
            _finish(state, "unsupported_container", error=reason, info=info)
        db.session.commit()
        _log("chapters_unsupported", error=reason, **fields)
        return state.chapter_status

    if action in WRITE_ACTIONS:
        busy = import_activity(part, media, info, opts["settle_minutes"])
        if busy:
            _defer(state, busy)
            db.session.commit()
            _log("chapters_deferred", reason=busy, **fields)
            return "deferred"

    if action in {"auto", "generate", "regenerate"} and _transcriber_missing(
        info, config
    ):
        # A setup problem, not a property of this file: waiting beats quietly
        # writing Navigation chapters across the whole library.
        _defer(state, TRANSCRIBER_MISSING)
        db.session.commit()
        _log("chapters_deferred", reason=state.chapter_error, **fields)
        return "deferred"

    analysis = analyse(
        info,
        config,
        opts,
        every_method=action == "preview",
        llm=llm,
        context=_context(part, media),
        find_recap=_recap_finder(state, part) if part.kind == "episode" else None,
    )
    if (analysis.get("transcript") or {}).get("key"):
        state.transcript_key = analysis["transcript"]["key"]
    fields |= {
        "attempts": analysis["attempts"],
        "fallback_used": analysis["chosen"],
    }
    if action == "preview" or (action == "auto" and config["processing"]["dry_run"]):
        state.last_preview_json = _preview_record(info, analysis, config, ours)
        if action == "auto":
            _finish(state, "dry_run", info=info)
        else:
            state.job_state, state.requested_action = "idle", None
            state.chapter_error = None
        db.session.commit()
        _log(
            "chapters_previewed",
            chapters=(analysis["plans"].get(analysis["chosen"]) or {}).get("chapters"),
            processing_seconds=round(time.monotonic() - started, 1),
            **fields,
        )
        return state.chapter_status
    if (
        analysis.get("language_model_unavailable")
        and action in {"auto", "generate", "regenerate"}
        and state.attempts + 1 < MAX_ATTEMPTS
    ):
        # Usually Ollama out of GPU memory or restarting. The transcript and
        # scene changes are cached, so trying again later costs little, and
        # Smart Chapters are worth the wait over Navigation chapters.
        state.attempts += 1
        _defer(state, "Ollama did not answer; Smart Chapters will try again")
        db.session.commit()
        _log("chapters_deferred", reason=state.chapter_error, **fields)
        return "deferred"
    if not analysis["chosen"]:
        errors = "; ".join(
            f"{a['method']}: {a['error']}" for a in analysis["attempts"] if not a["ok"]
        )
        _fail(state, f"No chapter method succeeded ({errors})", info=info)
        db.session.commit()
        _log("chapters_failed", error=state.chapter_error, **fields)
        return state.chapter_status
    plan = analysis["plans"][analysis["chosen"]]
    return _write(
        state, part, media, info, plan, analysis, config, ours, fields, started
    )


def _scan_result(state, info, config):
    if info.chapters and not curatarr_wrote(state, info):
        status, source = "existing", "embedded"
    elif info.chapters:
        status, source = state.chapter_status, state.chapter_source
    elif info.duration < config["minimum_duration_minutes"] * 60:
        status, source = "skipped_short", None
    elif not info.matroska:
        status, source = "unsupported_container", None
    else:
        status, source = (
            state.chapter_status
            if state.chapter_status in {"failed", "dry_run", "removed"}
            else "not_processed",
            None,
        )
    error = state.chapter_error if status == "failed" else None
    if status == "unsupported_container":
        error = can_write(info)[1]
    state.chapter_status = status
    state.chapter_source = source
    state.chapter_count = len(info.chapters)
    state.chapter_error = error
    state.duration_seconds = info.duration
    state.container = info.format_name[:60]
    state.job_state, state.requested_action = "idle", None
    db.session.commit()
    return status


def _preview_record(info, analysis, config, ours):
    chosen = analysis and analysis["chosen"]
    plan = analysis["plans"].get(chosen) if analysis else None
    return {
        "generated_at": utcnow().isoformat(),
        "generation_version": GENERATION_VERSION,
        "file": info.as_dict(),
        "existing_chapters": info.chapters,
        "existing_written_by_curatarr": ours,
        "too_short": analysis is None,
        "minimum_duration_minutes": config["minimum_duration_minutes"],
        "mode": config["mode"] if config["enabled"] else "disabled",
        "analysis": analysis,
        "write": write_operation(info, _plan_chapters(plan)) if plan else None,
        "writable": can_write(info)[1] or "yes",
    }


def _refresh_jellyfin(part, media):
    item_id = jellyfin_item_id(part, media)
    if not item_id:
        return "No Jellyfin item is linked to this file"
    try:
        client("jellyfin").refresh_item(item_id)
    except IntegrationError as exc:
        return f"Jellyfin refresh failed: {exc}"
    return None


def _write(state, part, media, info, plan, analysis, config, ours, fields, started):
    chapters = _plan_chapters(plan)
    method = plan["method"]
    try:
        validate_chapters(chapters, info.duration)
        if file_identity(info.path) != (info.size, info.mtime):
            _defer(state, "The file changed while chapters were being planned")
            db.session.commit()
            return "deferred"
        if info.chapters and not ours and not state.original_chapters_xml:
            # preserve_existing is off: keep the originals so removal restores them.
            state.original_chapters_xml = export_chapters_xml(info.path)
            if not state.original_chapters_xml:
                raise MediaToolError("Could not back up the existing chapters")
            db.session.commit()
        write_chapters(info.path, chapters)
        after = probe(info.path)
    except (MediaToolError, InvalidChapters, OSError) as exc:
        _fail(state, f"Writing chapters failed: {exc}", info=info)
        db.session.commit()
        _log("chapters_failed", error=str(exc), **fields)
        return state.chapter_status
    problems = []
    if after.stream_signature != info.stream_signature:
        problems.append("the stream list changed")
    if abs(after.duration - info.duration) > 0.5:
        problems.append("the duration changed")
    expected = fingerprint([c.as_dict() for c in chapters])
    if fingerprint(after.chapters) != expected:
        problems.append("the chapters read back differently")
    if problems:
        _fail(
            state,
            "Verification after writing failed: " + ", ".join(problems),
            info=after,
            retry=False,
        )
        db.session.commit()
        _log("chapters_verify_failed", error=state.chapter_error, **fields)
        return state.chapter_status
    status = GENERATED_STATUSES[method]
    _finish(state, status, info=after, source=method, count=len(chapters))
    state.attempts = 0
    state.chapter_generation_version = GENERATION_VERSION
    state.chapter_config_hash = config_hash(config)
    state.written_fingerprint = fingerprint(after.chapters)
    state.written_chapters_json = after.chapters
    refresh_error = _refresh_jellyfin(part, media)
    if analysis.get("transcript") and part.kind == "episode":
        _record_recap(state, part, media, analysis.get("recap"))
    state.last_preview_json = {
        **_preview_record(info, analysis, config, ours),
        "written_at": utcnow().isoformat(),
        "jellyfin_refresh": refresh_error or "requested",
    }
    audit(
        "chapters_generated",
        media.id,
        f"Added {len(chapters)} {method.replace('_', '-')} chapters to "
        f"{_part_label(part)}.",
        f"chapters:{state.id}:{state.written_fingerprint[:16]}:{int(after.mtime)}",
        payload={
            "file": info.path,
            "method": method,
            "attempts": analysis["attempts"],
            "chapters": [c.as_dict() for c in chapters],
            "jellyfin_refresh": refresh_error or "requested",
        },
    )
    db.session.commit()
    _log(
        "chapters_written",
        chapter_count=len(chapters),
        chapters=[f"{timestamp(c.start)} {c.name}" for c in chapters],
        processing_seconds=round(time.monotonic() - started, 1),
        write_result="verified",
        jellyfin_refresh=refresh_error or "requested",
        **fields,
    )
    return status


def _remove(state, part, media, info, ours, fields, started):
    if not ours:
        _fail(
            state,
            "These chapters were not written by Curatarr (or were changed since), "
            "so they are left alone",
            info=info,
            retry=False,
        )
        db.session.commit()
        return state.chapter_status
    writable, reason = can_write(info)
    if not writable:
        _fail(state, reason, info=info, retry=False)
        db.session.commit()
        return state.chapter_status
    settle = options()["settle_minutes"]
    busy = import_activity(part, media, info, settle)
    if busy:
        _defer(state, busy)
        db.session.commit()
        return "deferred"
    try:
        if state.original_chapters_xml:
            restore_chapters_xml(info.path, state.original_chapters_xml)
        else:
            remove_chapters(info.path)
        after = probe(info.path)
    except (MediaToolError, OSError) as exc:
        _fail(state, f"Removing chapters failed: {exc}", info=info, retry=False)
        db.session.commit()
        return state.chapter_status
    if after.stream_signature != info.stream_signature:
        _fail(state, "Verification failed: the stream list changed", info=after)
        db.session.commit()
        return state.chapter_status
    restored = bool(state.original_chapters_xml)
    _finish(
        state,
        "existing" if restored else "removed",
        info=after,
        source="embedded" if restored else None,
        count=len(after.chapters),
    )
    state.written_fingerprint = None
    state.written_chapters_json = None
    state.original_chapters_xml = None
    refresh_error = _refresh_jellyfin(part, media)
    audit(
        "chapters_removed",
        media.id,
        (
            "Restored the original chapters of "
            if restored
            else "Removed Curatarr's chapters from "
        )
        + f"{_part_label(part)}.",
        f"chapters-remove:{state.id}:{int(after.mtime)}",
        payload={"file": info.path, "command": remove_command(info.path)},
    )
    db.session.commit()
    _log(
        "chapters_removed",
        restored_original=restored,
        processing_seconds=round(time.monotonic() - started, 1),
        jellyfin_refresh=refresh_error or "requested",
        **fields,
    )
    return state.chapter_status


def _context(part, media):
    """Names the title for the language model (helps it name chapters)."""
    if part.kind == "episode":
        return f'"{media.title}" {_part_label(part)}'
    return f'the film "{media.title}"'


# --- Subtitles ---------------------------------------------------------------


def _chain_subtitles(state_id):
    """After a chapter job, add subtitles while the transcript is fresh."""
    db.session.expire_all()
    state = db.session.get(ChapterState, state_id)
    if not state or state.job_state != "idle" or state.subtitle_status is not None:
        return
    try:
        enabled = config_for(state.media.library_id)["subtitles"]["enabled"]
    except ChapterConfigError:
        return
    if enabled:
        state.job_state = "queued"
        state.requested_action = "subtitles"
        state.requested_at = utcnow()
        db.session.commit()


def _smart(state) -> bool:
    """Recaps are found from transcripts, which only Smart libraries make."""
    try:
        config = config_for(state.media.library_id)
    except ChapterConfigError:
        return False
    return config["mode"] == "smart" and config["semantic"]["enabled"]


def _chain_recap(state_id):
    """After an episode's other jobs, check it for a recap (once)."""
    db.session.expire_all()
    state = db.session.get(ChapterState, state_id)
    if (
        not state
        or state.job_state != "idle"
        or state.markers_checked_at is not None
        or state.part is None
        or state.part.kind != "episode"
        or not _smart(state)
    ):
        return
    state.job_state, state.requested_action = "queued", "recap"
    state.requested_at = utcnow()
    db.session.commit()


def _subtitles_done(state, status, *, error=None, fields=None):
    state.subtitle_status = status
    state.subtitle_error = error
    state.subtitle_processed_at = utcnow()
    state.job_state = "idle"
    state.requested_action = None
    state.next_attempt_at = None
    db.session.commit()
    _log(f"subtitles_{status}", error=error, **(fields or {}))
    return status


def _ours(state, target) -> bool:
    if not state.subtitle_fingerprint or not target.exists():
        return False
    try:
        return digest(target.read_text(encoding="utf-8")) == state.subtitle_fingerprint
    except (OSError, UnicodeDecodeError):
        return False


def run_subtitles(state, action, part, media, path, config, opts) -> str:
    """Write (or remove) the generated English subtitle file for one video."""
    fields = {"state_id": state.id, "action": action, "file": path}
    if not path or not file_exists(path):
        return _subtitles_done(
            state, "failed", error="The media file was not found", fields=fields
        )
    target = generated_path(path)
    ours = _ours(state, target)
    if action == "remove_subtitles":
        if not ours:
            return _subtitles_done(
                state,
                state.subtitle_status or "failed",
                error="That subtitle file was not written by Curatarr, or was "
                "changed since, so it is left alone",
                fields=fields,
            )
        try:
            target.unlink()
        except OSError as exc:
            return _subtitles_done(state, "failed", error=str(exc), fields=fields)
        state.subtitle_fingerprint = None
        state.subtitle_path = None
        _refresh_jellyfin(part, media)
        audit(
            "subtitles_removed",
            media.id,
            f"Removed Curatarr's generated subtitles from {_part_label(part)}.",
            f"subtitles-remove:{state.id}:{utcnow().isoformat()}",
            payload={"file": str(target)},
        )
        return _subtitles_done(state, "removed", fields=fields)
    try:
        info = probe(path)
    except (MediaToolError, OSError) as exc:
        return _subtitles_done(state, "failed", error=str(exc), fields=fields)
    if target.exists() and not ours:
        return _subtitles_done(
            state,
            "existing",
            error=f"{target.name} already exists and was not written by Curatarr",
            fields=fields,
        )
    if not ours:
        if embedded_english(info.streams):
            return _subtitles_done(state, "existing", fields=fields)
        sidecars = sidecar_english(path)
        if sidecars:
            return _subtitles_done(state, "existing", fields=fields)
    busy = import_activity(part, media, info, opts["settle_minutes"])
    if busy and "playing" not in busy:  # a file beside the video is fine to add
        state.job_state = "queued"
        state.next_attempt_at = utcnow() + timedelta(minutes=DEFER_MINUTES)
        state.subtitle_error = f"Waiting: {busy}"
        db.session.commit()
        return "deferred"
    started = time.monotonic()
    try:
        from .transcribe import transcribe

        segments, details = transcribe(
            info,
            config["semantic"]["model"],
            opts["whisper_device"],
            cache_dir() / "transcripts",
        )
    except (MediaToolError, RuntimeError) as exc:
        return _subtitles_done(state, "failed", error=str(exc), fields=fields)
    language = details.get("language")
    if language and language != "en":
        return _subtitles_done(
            state,
            "skipped_language",
            error=f"The audio is not English (detected: {language})",
            fields=fields,
        )
    cues = cues_from(segments)
    if not cues:
        return _subtitles_done(state, "no_speech", fields=fields)
    text = srt_text(cues)
    partial = target.with_name(target.name + ".partial")
    try:
        partial.write_text(text, encoding="utf-8")
        os.replace(partial, target)
    except OSError as exc:
        _best_effort_unlink(partial)
        return _subtitles_done(state, "failed", error=str(exc), fields=fields)
    state.subtitle_fingerprint = digest(text)
    state.subtitle_path = str(target)
    refresh_error = _refresh_jellyfin(part, media)
    audit(
        "subtitles_generated",
        media.id,
        f"Added generated English subtitles ({len(cues)} lines) to "
        f"{_part_label(part)}.",
        f"subtitles:{state.id}:{state.subtitle_fingerprint[:16]}",
        payload={
            "file": str(target),
            "cues": len(cues),
            "transcript_cached": details.get("cached"),
            "jellyfin_refresh": refresh_error or "requested",
        },
    )
    fields |= {
        "cues": len(cues),
        "transcript_cached": details.get("cached"),
        "processing_seconds": round(time.monotonic() - started, 1),
    }
    return _subtitles_done(state, "generated", fields=fields)


def _best_effort_unlink(path):
    try:
        path.unlink()
    except OSError:
        pass


def queue_subtitles(library_id=None) -> int:
    """Queue every file not yet checked for subtitles (or whose check failed)."""
    count = 0
    for part in _parts_query(library_id):
        state = state_for(part)
        if state.job_state != "idle" or state.subtitle_status not in {
            None,
            "failed",
        }:
            continue
        state.job_state = "queued"
        state.requested_action = "subtitles"
        state.requested_at = utcnow()
        count += 1
    db.session.commit()
    return count


def _part_label(part):
    if part.kind == "episode":
        return f"S{part.season_number or 0:02}E{part.episode_number or 0:02}"
    return "the movie file"


# --- Dispatch ---------------------------------------------------------------

_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


def _alive():
    with _threads_lock:
        for key in [k for k, t in _threads.items() if not t.is_alive()]:
            del _threads[key]
        return dict(_threads)


def release_abandoned_jobs() -> int:
    """At worker start: requeue jobs the previous worker left running.

    Only the worker runs chapter jobs, so a claim on a running job at startup
    belongs to a process that has stopped. Waiting for it to expire held up
    every slot for up to ten minutes after each restart.
    """
    from sqlalchemy import delete

    from .models import JobLease

    abandoned = db.session.query(ChapterState).filter_by(job_state="running").all()
    for state in abandoned:
        db.session.execute(
            delete(JobLease).where(JobLease.scope == f"chapters:{state.id}")
        )
        state.job_state = "queued"
    db.session.commit()
    return len(abandoned)


def recover_stale_jobs():
    """Requeue jobs left running by a worker that stopped mid-job."""
    from .models import JobLease

    live = {
        row.scope
        for row in db.session.query(JobLease).filter(
            JobLease.scope.like("chapters:%"), JobLease.expires_at > utcnow()
        )
    }
    for state in db.session.query(ChapterState).filter_by(job_state="running"):
        if f"chapters:{state.id}" not in live and state.id not in _alive():
            state.job_state = "queued"
    db.session.commit()


def _due_jobs(limit, *, scans, recaps=False):
    """The next jobs to start: scans, recap checks, or everything else."""
    now = utcnow()
    query = db.session.query(ChapterState).filter(ChapterState.job_state == "queued")
    if scans:
        query = query.filter(ChapterState.requested_action == "scan")
    elif recaps:
        query = query.filter(ChapterState.requested_action == "recap")
        if _chapter_backlog():
            # Checking an episode with no saved transcript means transcribing its
            # opening and ending, which takes the GPU from chapter jobs (it
            # doubled their time). Until the backlog is done, only episodes that
            # need none go ahead, plus checks asked for from an episode's page.
            query = query.filter(
                or_(
                    ChapterState.transcript_key.isnot(None),
                    ChapterState.requested_at < now - timedelta(days=365),
                )
            )
    else:
        query = query.filter(
            or_(
                ChapterState.requested_action.is_(None),
                ChapterState.requested_action.notin_(("scan", "recap")),
            )
        )
    # Subtitles reuse a saved transcript and take seconds, and a redo reuses
    # its transcript too; neither should wait behind a backlog of thousands.
    priority = case(
        (ChapterState.requested_action == "subtitles", 0),
        (ChapterState.requested_action == "regenerate", 1),
        (ChapterState.requested_action == "recap", 1),
        (ChapterState.requested_action == "auto", 2),
        else_=3,
    )
    rows = (
        query.order_by(priority, ChapterState.requested_at).limit(limit * 4 + 100).all()
    )
    from . import dub_jobs

    dubs_on = not scans and not recaps and dub_jobs.enabled()
    return [
        row
        for row in rows
        if (not row.next_attempt_at or _as_datetime(row.next_attempt_at) <= now)
        and not dub_jobs.holds_chapters(row, dubs_on)
    ][:limit]


def _chapter_backlog() -> bool:
    """Chapter jobs (not checks or subtitles) are waiting or running."""
    return (
        db.session.query(ChapterState.id)
        .filter(
            ChapterState.job_state.in_(("queued", "running")),
            or_(
                ChapterState.requested_action.is_(None),
                ChapterState.requested_action.in_(("auto", "generate", "regenerate")),
            ),
        )
        .first()
        is not None
    )


class DatabaseBusy(RuntimeError):
    pass


def _run_one(app, state_id, llm):
    scope = f"chapters:{state_id}"
    try:
        owner = leases.acquire(scope, LEASE_SECONDS)
    except OperationalError as exc:
        db.session.rollback()
        raise DatabaseBusy from exc
    if not owner:
        return
    try:
        state = db.session.get(ChapterState, state_id)
        if not state or state.job_state != "queued":
            return
        action = state.requested_action
        state.job_state = "running"
        db.session.commit()
        with leases.keep_alive(scope, owner, LEASE_SECONDS):
            run_job(state_id, llm=llm)
        if action in {"auto", "generate", "regenerate"}:
            _chain_subtitles(state_id)
        if action in {"auto", "generate", "regenerate", "subtitles"}:
            _chain_recap(state_id)
    except OperationalError as exc:
        # SQLite allows one writer; discovery holds a long write transaction.
        # Put the job back rather than failing it.
        db.session.rollback()
        _best_effort(lambda: _requeue(state_id))
        raise DatabaseBusy from exc
    except Exception:
        db.session.rollback()
        app.logger.exception("Chapter job %s failed unexpectedly", state_id)
        _best_effort(lambda: _fail_unexpected(state_id))
    finally:
        # If this cannot be released now, it expires and the job is requeued.
        _best_effort(lambda: leases.release(scope, owner))


def _requeue(state_id):
    state = db.session.get(ChapterState, state_id)
    if state and state.job_state == "running":
        state.job_state = "queued"
        db.session.commit()


def _fail_unexpected(state_id):
    state = db.session.get(ChapterState, state_id)
    if state and state.job_state == "running":
        _fail(state, "Unexpected error; see the worker log")
        db.session.commit()


def _best_effort(write):
    try:
        write()
    except OperationalError:
        db.session.rollback()


def _run_leased(app, state_ids, llm=None):
    with app.app_context():
        for state_id in state_ids:
            try:
                _run_one(app, state_id, llm)
            except DatabaseBusy:
                app.logger.warning(
                    "Chapter jobs paused: the database is busy; resuming next cycle"
                )
                return


def playback_active() -> bool:
    """Whether anyone is playing something in Jellyfin right now."""
    try:
        sessions = client("jellyfin").sessions() or []
    except IntegrationError:
        return False  # cannot tell; do not hold work back indefinitely
    return any(session.get("NowPlayingItem") for session in sessions)


def _set_paused(pause: bool):
    if throttle.set_paused(pause):
        set_setting("chapters_paused_since", utcnow().isoformat() if pause else None)
        _log("chapters_paused" if pause else "chapters_resumed")


def playback_sessions() -> list[dict]:
    try:
        return client("jellyfin").sessions() or []
    except IntegrationError:
        return []  # cannot tell; don't hold work back indefinitely


def _record_limit(limit: int, reason: str):
    """Keep the current job limit for the Activity card (written only on change)."""
    current = {"limit": limit, "reason": reason}
    if setting("chapter_job_limit") != current:
        set_setting("chapter_job_limit", current)


def dispatch(*, synchronous=False, llm=None) -> int:
    """Start queued chapter jobs up to the configured concurrency.

    Scans (ffprobe only) run in one batch thread of their own so a library scan
    does not wait behind, or crowd out, transcription jobs.
    """
    from . import adaptive

    recover_stale_jobs()
    try:
        opts = options()
    except ChapterConfigError:
        opts = GLOBAL_DEFAULTS
    has_work = (
        db.session.query(ChapterState.id)
        .filter(ChapterState.job_state.in_(("queued", "running")))
        .first()
        is not None
    )
    if not has_work:
        _set_paused(False)
        return 0
    playing, transcoding = adaptive.viewers(playback_sessions())
    if not opts["pause_during_playback"]:
        playing = transcoding = 0  # "make room for people watching" is off
    reading = adaptive.Reading(
        adaptive.cpu_use(), adaptive.gpu_use(), playing, transcoding
    )
    busy = (
        db.session.query(func.count(ChapterState.id))
        .filter(
            ChapterState.job_state == "running",
            ChapterState.requested_action != "scan",
        )
        .scalar()
    )
    decision = adaptive.controller.decide(
        reading, most=opts["concurrency"], running=busy, waiting=True
    )
    _record_limit(decision.limit, decision.reason)
    _set_paused(decision.suspend)
    if decision.suspend:
        return 0
    app = current_app._get_current_object()
    started = 0
    running = _alive()
    if "scan" not in running:
        scans = [row.id for row in _due_jobs(SCAN_BATCH, scans=True)]
        if scans:
            started += len(scans)
            if synchronous:
                _run_leased(app, scans, llm)
            else:
                _start("scan", app, scans, llm)
    # Counted in the database, so the limit holds across worker processes.
    busy = max(busy, len([k for k in running if k != "scan"]))
    free = decision.limit - busy
    if free <= 0:
        return started
    if "recap" not in running:
        # Recap checks often finish in milliseconds (a series without recaps is
        # skipped), so one at a time per 15-second cycle would crawl. They run
        # back to back in one slot, leaving the rest to chapter jobs.
        recaps = [row.id for row in _due_jobs(RECAP_BATCH, scans=False, recaps=True)]
        if recaps:
            started += len(recaps)
            free -= 1
            if synchronous:
                _run_leased(app, recaps, llm)
            else:
                _start("recap", app, recaps, llm)
    if free <= 0:
        return started
    for row in _due_jobs(free, scans=False):
        if row.id in running:
            continue
        started += 1
        if synchronous:
            _run_leased(app, [row.id], llm)
        else:
            _start(row.id, app, [row.id], llm)
    return started


def _start(key, app, state_ids, llm):
    thread = threading.Thread(
        target=_run_leased,
        args=(app, state_ids, llm),
        name=f"curatarr-chapters:{key}",
        daemon=True,
    )
    with _threads_lock:
        _threads[key] = thread
    thread.start()


# --- Maintenance ------------------------------------------------------------


def _parts_query(library_id=None):
    query = (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .filter(
            Library.media_type.in_(("tv", "movies")),
            MediaIdentity.missing_since.is_(None),
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
        )
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return query


def queue_scan(library_id=None) -> int:
    count = 0
    for part in _parts_query(library_id):
        state = state_for(part)
        if state.job_state != "idle":
            continue
        state.job_state = "queued"
        state.requested_action = "scan"
        state.requested_at = utcnow()
        count += 1
    db.session.commit()
    return count


def file_locations_known() -> bool:
    return _parts_query().first() is not None


def request_scan_after_discovery(library_id=None):
    set_setting("chapter_scan_requested", {"library_id": library_id})


def run_requested_scan() -> int:
    """After discovery: start a scan that was asked for before paths existed."""
    requested = setting("chapter_scan_requested")
    if not requested:
        return 0
    set_setting("chapter_scan_requested", None)
    return queue_scan(requested.get("library_id"))


def needs_chapters_query(library_id=None):
    query = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.chapter_status.in_(("not_processed", "dry_run", "failed")),
            ChapterState.duration_seconds.isnot(None),
            MediaPart.has_file.is_(True),
        )
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return query


def needs_subtitles_query(library_id=None):
    """Files not yet checked for subtitles, or whose check failed (mirrors queue_subtitles)."""
    return (
        _parts_query(library_id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            or_(
                ChapterState.subtitle_status.is_(None),
                ChapterState.subtitle_status == "failed",
            ),
            or_(ChapterState.job_state.is_(None), ChapterState.job_state == "idle"),
        )
    )


ACTION_LABELS = {
    "scan": "Scan",
    "auto": "Automatic",
    "generate": "Generate",
    "regenerate": "Regenerate",
    "remove": "Remove",
    "preview": "Preview",
    "subtitles": "Subtitles",
    "remove_subtitles": "Remove subtitles",
    "recap": "Check skip markers",
}


# Fast jobs that never set chapter_processed_at; they don't count towards the forecast.
FORECAST_EXCLUDED_ACTIONS = ("scan", "subtitles", "remove_subtitles", "recap")
# A day evens out evenings when jobs pause for viewers and long films transcribing.
FORECAST_WINDOW = timedelta(hours=24)
FORECAST_LONG_WINDOW = timedelta(hours=72)
FORECAST_MIN_SAMPLES = 3


def job_forecast(now=None) -> dict | None:
    """Progress and estimated finish for the queued chapter jobs, from the recent pace.

    The batch is everything queued since the oldest job still waiting; the pace is
    jobs finished per hour over the last 24 hours (72 when that's too few to tell).
    """
    now = now or utcnow()
    pending = db.session.query(ChapterState).filter(
        ChapterState.job_state.in_(("queued", "running")),
        or_(
            ChapterState.requested_action.is_(None),
            ChapterState.requested_action.notin_(FORECAST_EXCLUDED_ACTIONS),
        ),
    )
    remaining = pending.count()
    if not remaining:
        return None
    started = (
        _as_datetime(
            pending.with_entities(func.min(ChapterState.requested_at)).scalar()
        )
        or now
    )

    def finished_since(moment):
        return (
            db.session.query(func.count(ChapterState.id))
            .filter(ChapterState.chapter_processed_at >= moment)
            .scalar()
        )

    def video_since(moment):
        return (
            db.session.query(func.sum(ChapterState.duration_seconds))
            .filter(ChapterState.chapter_processed_at >= moment)
            .scalar()
        ) or 0

    done = finished_since(started)
    rate = video_rate = None
    for window in (FORECAST_WINDOW, FORECAST_LONG_WINDOW):
        since = max(now - window, started)
        hours = (now - since).total_seconds() / 3600
        count = finished_since(since)
        if count >= FORECAST_MIN_SAMPLES and hours > 0:
            rate = count / hours
            video_rate = video_since(since) / hours or None
            break
    # Paced by hours of video rather than files, so a run of long films after a
    # run of short episodes doesn't read as a slowdown. Files of unknown length
    # count as the average of those known.
    known, video_left = pending.with_entities(
        func.count(ChapterState.duration_seconds),
        func.sum(ChapterState.duration_seconds),
    ).one()
    hours_left = None
    if video_rate and known:
        hours_left = video_left * remaining / known / video_rate
    elif rate:
        hours_left = remaining / rate
    eta = now + timedelta(hours=hours_left) if hours_left is not None else None
    return {
        "remaining": remaining,
        "done": done,
        "percent": round(100 * done / (done + remaining), 1),
        "per_hour": rate,
        "video_per_hour": video_rate / 3600 if video_rate else None,
        "finishes_at": eta,
        "left": _duration_words(eta - now) if eta else None,
        "started": started,
    }


def _duration_words(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 1)
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (
            f" {hours} h" if hours else ""
        )
    if hours:
        return f"{hours} h" + (f" {minutes} min" if minutes and hours < 3 else "")
    return f"{minutes} min"


def job_activity(recent=10) -> dict:
    """What the chapter jobs are doing now, for the maintenance page."""
    running = (
        db.session.query(ChapterState)
        .filter(ChapterState.job_state == "running")
        .order_by(ChapterState.updated_at)
        .all()
    )
    queued = dict(
        db.session.query(ChapterState.requested_action, func.count(ChapterState.id))
        .filter(ChapterState.job_state == "queued")
        .group_by(ChapterState.requested_action)
        .all()
    )
    waiting = (
        db.session.query(ChapterState)
        .filter(
            ChapterState.job_state == "queued",
            ChapterState.next_attempt_at.isnot(None),
        )
        .order_by(ChapterState.next_attempt_at)
        .limit(5)
        .all()
    )
    finished = (
        db.session.query(ChapterState)
        .filter(
            ChapterState.job_state == "idle",
            ChapterState.chapter_processed_at.isnot(None),
        )
        .order_by(ChapterState.chapter_processed_at.desc())
        .limit(recent)
        .all()
    )
    # Subtitle existence checks finish in milliseconds, so "running" is almost
    # never caught mid-job; show the most recent ones directly instead of
    # saying nothing is happening.
    subtitles_finished = (
        db.session.query(ChapterState)
        .filter(
            ChapterState.subtitle_status.isnot(None),
            ChapterState.subtitle_processed_at.isnot(None),
        )
        .order_by(ChapterState.subtitle_processed_at.desc())
        .limit(recent)
        .all()
    )
    last_recap = (
        db.session.query(ChapterState)
        .filter(ChapterState.markers_checked_at.isnot(None))
        .order_by(ChapterState.markers_checked_at.desc())
        .first()
    )
    recent_cutoff = utcnow() - timedelta(seconds=20)

    def recent(value) -> bool:
        # A row read earlier in this request (say, while still running) keeps its
        # old values even when a later query returns it again, so a time can
        # still be missing here.
        moment = _as_datetime(value)
        return moment is not None and moment >= recent_cutoff

    recently_active = bool(subtitles_finished) and recent(
        subtitles_finished[0].subtitle_processed_at
    )
    return {
        "paused_since": _as_datetime(setting("chapters_paused_since")),
        "job_limit": setting("chapter_job_limit"),
        "running": running,
        "recently_active": recently_active,
        "recap_active": last_recap
        if last_recap and recent(last_recap.markers_checked_at)
        else None,
        "subtitles_finished": subtitles_finished,
        "queued": {ACTION_LABELS.get(k, k): v for k, v in queued.items()},
        "queued_total": sum(queued.values()),
        "waiting": waiting,
        "finished": finished,
        "forecast": job_forecast(),
    }


def file_label(state: ChapterState) -> str:
    part, media = state.part, state.media
    if part.kind == "episode":
        return f"{media.title} · S{part.season_number or 0:02}E{part.episode_number or 0:02}"
    return media.title


def maintenance_failed_count() -> int:
    return (
        db.session.query(func.count(ChapterState.id))
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(ChapterState.chapter_status == "failed", MediaPart.has_file.is_(True))
        .scalar()
    )


def maintenance_summary(library_id=None) -> dict:
    parts = _parts_query(library_id).count()
    rows = (
        db.session.query(ChapterState.chapter_status, func.count(ChapterState.id))
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(ChapterState.duration_seconds.isnot(None), MediaPart.has_file.is_(True))
    )
    if library_id:
        rows = rows.filter(MediaIdentity.library_id == library_id)
    by_status = dict(rows.group_by(ChapterState.chapter_status).all())
    scanned = sum(by_status.values())
    needing = sum(by_status.get(s, 0) for s in ("not_processed", "dry_run", "failed"))
    generated = sum(by_status.get(s, 0) for s in GENERATED_STATUSES.values())
    smart_libraries = _smart_library_ids()
    needing_smart = (
        needs_chapters_query(library_id)
        .filter(MediaIdentity.library_id.in_(smart_libraries or {""}))
        .count()
    )
    queued = (
        db.session.query(func.count(ChapterState.id))
        .filter(
            ChapterState.job_state.in_(("queued", "running")),
            # Chapter jobs only: skip-marker checks have their own count.
            or_(
                ChapterState.requested_action.is_(None),
                ChapterState.requested_action.notin_(FORECAST_EXCLUDED_ACTIONS),
            ),
        )
        .scalar()
    )
    subtitle_rows = (
        db.session.query(ChapterState.subtitle_status, func.count(ChapterState.id))
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(ChapterState.subtitle_status.isnot(None), MediaPart.has_file.is_(True))
    )
    if library_id:
        subtitle_rows = subtitle_rows.filter(MediaIdentity.library_id == library_id)
    subtitles = dict(subtitle_rows.group_by(ChapterState.subtitle_status).all())
    needing_subtitles = needs_subtitles_query(library_id).count()
    to_check = recaps_to_check(library_id)
    checked = markers_checked(library_id)
    existing = by_status.get("existing", 0)
    with_subtitles = subtitles.get("existing", 0) + subtitles.get("generated", 0)
    from . import dub_jobs

    dub_counts = dub_jobs.counts() if not library_id else {}
    return {
        **dub_counts,
        # Coverage: how much of what can have each thing now has it.
        "chapters_have": existing + generated,
        "chapters_possible": existing + generated + needing,
        "subtitles_have": with_subtitles,
        "subtitles_possible": with_subtitles + needing_subtitles,
        "markers_checked": checked,
        "markers_possible": checked + to_check,
        "subtitles_generated": subtitles.get("generated", 0),
        "subtitles_existing": subtitles.get("existing", 0),
        "subtitles_checked": sum(subtitles.values()),
        "files": parts,
        "scanned": scanned,
        "not_scanned": max(parts - scanned, 0),
        "existing": existing,
        "generated": generated,
        "needing": needing,
        "needing_subtitles": needing_subtitles,
        "short": by_status.get("skipped_short", 0),
        "unsupported": by_status.get("unsupported_container", 0),
        "failed": by_status.get("failed", 0),
        "removed": by_status.get("removed", 0),
        "transcription_estimate": needing_smart,
        "smart_upgradable": smart_upgrade_query(library_id).count(),
        "recaps_to_check": to_check,
        "recaps_unqueued": recaps_to_check(library_id, unqueued=True),
        "recaps_found": recaps_found(library_id),
        "previews_found": previews_found(library_id),
        "markers_hidden": markers_hidden(library_id),
        "identity_issues": len(identity_issues(library_id)),
        "queued": queued,
    }


def _smart_library_ids() -> set:
    smart = set()
    for library in db.session.query(Library):
        try:
            config = config_for(library.id)
        except ChapterConfigError:
            continue
        if config["mode"] == "smart" and config["semantic"]["enabled"]:
            smart.add(library.id)
    return smart


def smart_upgrade_query(library_id=None):
    """Files in Smart libraries that got Navigation chapters instead.

    That happens when Ollama or transcription was unavailable at the time.
    Only chapters still exactly as Curatarr wrote them are listed.
    """
    query = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.chapter_status.in_(
                (GENERATED_STATUSES["scene_aligned"], GENERATED_STATUSES["fixed"])
            ),
            ChapterState.written_fingerprint.isnot(None),
            # Waiting only for subtitles counts: the redo adds them afterwards.
            or_(
                ChapterState.job_state == "idle",
                and_(
                    ChapterState.job_state == "queued",
                    ChapterState.requested_action == "subtitles",
                ),
            ),
            MediaPart.has_file.is_(True),
            MediaIdentity.library_id.in_(_smart_library_ids() or {""}),
        )
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return query


def queue_smart_upgrade(library_id=None) -> int:
    count = 0
    for state in smart_upgrade_query(library_id):
        state.job_state = "queued"
        state.requested_action = "regenerate"
        state.requested_at = utcnow()
        state.attempts = 0
        state.next_attempt_at = None
        count += 1
    db.session.commit()
    return count


def queue_selected(state_ids, action) -> int:
    if action not in {"preview", "generate", "regenerate"}:
        raise ValueError("Unsupported bulk action")
    count = 0
    for state in db.session.query(ChapterState).filter(ChapterState.id.in_(state_ids)):
        if state.job_state == "idle":
            state.job_state = "queued"
            state.requested_action = action
            state.requested_at = utcnow()
            state.attempts = 0
            state.next_attempt_at = None
            count += 1
    db.session.commit()
    return count


def queue_library(library_id, action="generate") -> int:
    ids = [state.id for state in needs_chapters_query(library_id)]
    return queue_selected(ids, action)


def describe(state: ChapterState | None) -> dict:
    """What the title page shows for one file."""
    if not state or state.chapter_status == "not_processed" and not state.chapter_count:
        label = (
            "None" if state and state.duration_seconds is not None else "Not checked"
        )
    elif state.chapter_status == "existing":
        label = "Existing"
    elif state.chapter_status in GENERATED_STATUSES.values():
        label = "Generated"
    else:
        label = state.chapter_status.replace("_", " ").capitalize()
    return {
        "label": label,
        "source": {
            "embedded": "Embedded",
            "semantic": "Semantic",
            "scene_aligned": "Scene-aligned",
            "fixed": "Fixed interval",
        }.get(state.chapter_source if state else None, "—"),
        "count": state.chapter_count if state else None,
        "processed_at": state.chapter_processed_at if state else None,
        "can_remove": bool(state and state.written_fingerprint),
        "subtitles": {
            None: "Not checked",
            "generated": "Generated",
            "existing": "Already has English subtitles",
            "skipped_language": "Not English audio",
            "no_speech": "No speech found",
            "removed": "Removed",
            "failed": "Failed",
        }.get(state.subtitle_status if state else None, "—"),
        "subtitles_ours": bool(state and state.subtitle_fingerprint),
        "queued": bool(state and state.job_state in {"queued", "running"}),
        "recap": (state.recap_start, state.recap_end)
        if state and state.recap_end is not None
        else None,
        "preview": (state.preview_start, state.preview_end)
        if state and state.preview_start is not None
        else None,
        "markers_fixed": len(state.markers_hidden or []) if state else 0,
        "dub": _dub_label(state.dub_result if state else None),
    }


def _dub_label(result) -> tuple[str, str] | None:
    """(text, tone) for a film's dub check, or None when there's nothing to say."""
    if not result or "verdict" not in result:
        return None
    if result.get("replaced"):
        return "Replaced: no English audio", "accent"
    if result["verdict"] == "no_english":
        return "No English audio", "danger"
    if result.get("removed"):
        return "Other languages removed", "on"
    if result.get("applied"):
        return "English audio fixed", "on"
    if result["verdict"] == "english":
        return "English audio", "on"
    return None


def environment_status() -> dict:
    """Which tools this host has, for the settings page."""
    from .mediafile import tool
    from .transcribe import available

    status = {
        "ffprobe": tool("ffprobe"),
        "ffmpeg": tool("ffmpeg"),
        "mkvpropedit": tool("mkvpropedit"),
        "mkvextract": tool("mkvextract"),
        "faster_whisper": available(),
        "ollama": None,
        "ollama_models": [],
    }
    try:
        from .semantic import LanguageModelError, Ollama

        status["ollama_models"] = Ollama(options()["ollama_url"], "probe").models()
        status["ollama"] = True
    except (LanguageModelError, ChapterConfigError, ValueError):
        status["ollama"] = False
    return status


# --- Recaps ------------------------------------------------------------------

RECAP_SIBLINGS = 60  # other episodes read for the series' closing line
RECAP_EARLIER = 3  # earlier episodes whose clips a recap may quote
RECAP_SKIP_AFTER = 4  # a series whose first episodes never say "previously on"


def _episode_order(part):
    return (part.season_number or 0, part.episode_number or 0)


def _transcript(key):
    from .transcribe import load

    if not key:
        return None
    hit = load(cache_dir() / "transcripts" / f"{key}.json")
    return hit[0] if hit else None


def _sibling_transcripts(state, part):
    """(openings of other episodes, full transcripts of the nearest earlier ones)."""
    from . import recaps

    here = _episode_order(part)
    rows = (
        db.session.query(ChapterState, MediaPart)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(
            ChapterState.media_identity_id == state.media_identity_id,
            ChapterState.id != state.id,
            or_(
                ChapterState.transcript_key.isnot(None),
                ChapterState.opening_key.isnot(None),
            ),
        )
        .all()
    )

    def distance(row):
        season, episode = _episode_order(row[1])
        return (abs(season - here[0]), abs(episode - here[1]))

    rows.sort(key=distance)
    openings, earlier = [], []
    for sibling, sibling_part in rows[:RECAP_SIBLINGS]:
        full = _transcript(sibling.transcript_key)
        segments = full or _transcript(sibling.opening_key)
        if segments:
            openings.append(recaps.opening_lines(segments))
        if (
            full
            and _episode_order(sibling_part) < here
            and len(earlier) < RECAP_EARLIER
        ):
            earlier.append(full)
    return openings, earlier


def _recap_finder(state, part):
    """A find_recap callable for analyse(), reading the rest of the series."""
    from . import recaps

    def find(segments, scenes, _model):
        # No model for recap ends: tested on The Last Man on Earth, it pushed
        # every montage recap ~30 s into the new episode (and confirmed it),
        # while the last quoted clip alone never ran long.
        openings, earlier = _sibling_transcripts(state, part)
        return recaps.find_recap(
            segments,
            series_openings=openings,
            earlier=recaps.earlier_grams(earlier),
            scenes=scenes,
        )

    return find


def _record_recap(state, part, media, recap: dict | None):
    """Store what was found and mark it in Intro Skipper (best effort)."""
    state.recap_checked_at = utcnow()
    if recap:
        state.recap_start, state.recap_end = recap["start"], recap["end"]
        state.recap_source = recap["source"]
    else:
        state.recap_start = state.recap_end = None
        state.recap_source = "none" if state.recap_source != "none" else "rechecked"
    try:
        _mark_segment(state, part, media, "Recap")
    except IntegrationError as exc:
        _log("recap_segment_failed", state_id=state.id, error=str(exc))


SEGMENT_FIELDS = {"Recap": "recap", "Preview": "preview"}


def _mark_segment(state, part, media, kind, *, clear_guesses=False):
    """Make Intro Skipper's recap or preview for this episode match Curatarr's.

    Ours replaces any the plugin guessed (its recaps are usually cold opens).
    With nothing found, a segment Curatarr added earlier is removed, and the
    plugin's guesses too when ``clear_guesses`` (the episode plainly has none).
    """
    from .integrations import IntroSkipper

    field = SEGMENT_FIELDS[kind]
    start = getattr(state, f"{field}_start")
    end = getattr(state, f"{field}_end")
    ours = getattr(state, f"{field}_segment_id")
    item_id = jellyfin_item_id(part, media)
    if not item_id:
        return
    plugin = IntroSkipper(client("jellyfin"))
    segments = plugin.segments(item_id)
    if segments is None:
        return  # Intro Skipper isn't installed
    found = end is not None
    current = [s for s in segments if s.get("Type") == kind and not s.get("Suppressed")]
    keep = next(
        (
            s
            for s in current
            if found
            and s.get("Id") == ours
            and abs(s.get("Start", 0) - start) < 0.5
            and abs(s.get("End", 0) - end) < 0.5
        ),
        None,
    )
    for segment in current:
        if segment is not keep and (
            found or clear_guesses or segment.get("Id") == ours
        ):
            plugin.hide(item_id, segment["Id"])
            if segment.get("Id") != ours:  # the plugin's guess: listed for review
                state.markers_hidden = [
                    *(state.markers_hidden or []),
                    {
                        "id": segment["Id"],
                        "type": kind,
                        "start": round(segment.get("Start", 0), 1),
                        "end": round(segment.get("End", 0), 1),
                        "source": segment.get("Source"),
                        "reason": f"replaced by Curatarr's {kind.lower()}"
                        if found
                        else 'a guessed recap on an episode with no "previously on"',
                        "at": utcnow().isoformat(),
                    },
                ]
    if keep is not None:
        return
    setattr(state, f"{field}_segment_id", None)
    if found:
        setattr(state, f"{field}_segment_id", plugin.add(item_id, kind, start, end))
        _log(
            f"{field}_marked",
            state_id=state.id,
            start=start,
            end=end,
            source=getattr(state, f"{field}_source"),
        )


def _mark_recap_segment(state, part, media):
    _mark_segment(state, part, media, "Recap")


def _series_without(state, field) -> bool:
    """The series' first checked episodes never announced a recap/preview."""
    rows = (
        db.session.query(getattr(ChapterState, f"{field}_source"))
        .filter(
            ChapterState.media_identity_id == state.media_identity_id,
            getattr(ChapterState, f"{field}_checked_at").isnot(None),
        )
        .all()
    )
    return len(rows) >= RECAP_SKIP_AFTER and all(
        source in {"none", "rechecked", "no opener"} for (source,) in rows
    )


def _series_without_recaps(state) -> bool:
    return _series_without(state, "recap")


class _Episode:
    """What the skip-marker checks need, each fetched once and only if needed."""

    def __init__(self, state, part, media, info, config, opts, llm):
        self.state, self.part, self.media, self.info = state, part, media, info
        self.config, self.opts = config, opts
        self._llm, self._scenes, self._full = llm, None, False
        self.model_name = config["semantic"]["model"]

    def full(self):
        """The whole transcript when it's saved, else None (never transcribes)."""
        from .transcribe import cache_key

        if self._full is False:
            segments = _transcript(self.state.transcript_key)
            if segments is None:
                key = cache_key(self.info, self.model_name)
                segments = _transcript(key)
                if segments is not None:
                    self.state.transcript_key = key
            self._full = segments
        return self._full

    def span(self, start, end, *, transcribe_missing=True):
        """Transcript lines in [start, end): from the saved transcript, the saved
        opening, or by transcribing just that part."""
        from .transcribe import OPENING_SECONDS, cache_key, transcribe

        start, end = max(0.0, start), min(self.info.duration, end)
        full = self.full()
        if full is not None:
            return [s for s in full if start <= s.start < end]
        if end <= OPENING_SECONDS:
            opening = _transcript(self.state.opening_key)
            if opening is not None:
                return [s for s in opening if start <= s.start < end]
            window, opening_only = None, True
        else:
            window, opening_only = (int(start), int(end) + 1), False
        if not transcribe_missing:
            return None
        segments, _details = transcribe(
            self.info,
            self.model_name,
            self.opts["whisper_device"],
            cache_dir() / "transcripts",
            opening=opening_only,
            window=window,
        )
        if opening_only:
            self.state.opening_key = cache_key(self.info, self.model_name, opening=True)
        return [s for s in segments if start <= s.start < end]

    def scenes(self):
        """Saved scene changes only: detecting them for a whole episode takes
        minutes, far more than snapping a recap's end to a cut is worth."""
        if self._scenes is None:
            path = _scene_cache_path(self.info, self.opts["scene_threshold"])
            try:
                self._scenes = [row[0] for row in json.loads(path.read_text())]
            except (OSError, ValueError, IndexError, TypeError):
                self._scenes = []
        return self._scenes

    def model(self):
        from .semantic import LanguageModelError

        if self._llm is None:
            try:
                self._llm = _language_model(self.config, self.opts)
            except LanguageModelError:
                self._llm = False  # the phrase and clip evidence don't need it
        return self._llm or None

    def neighbours(self, *, later: bool, count: int):
        """Full transcripts of the nearest earlier (or later) episodes."""
        here = _episode_order(self.part)
        rows = (
            db.session.query(ChapterState, MediaPart)
            .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
            .filter(
                ChapterState.media_identity_id == self.state.media_identity_id,
                ChapterState.id != self.state.id,
                ChapterState.transcript_key.isnot(None),
            )
            .all()
        )
        rows = [r for r in rows if (_episode_order(r[1]) > here) == later]
        rows.sort(key=lambda r: _episode_order(r[1]), reverse=not later)
        found = []
        for sibling, _part in rows:
            segments = _transcript(sibling.transcript_key)
            if segments:
                found.append(segments)
            if len(found) == count:
                break
        return found

    def others(self, limit=12):
        """Other episodes' saved transcripts (whole or openings), nearest first."""
        here = _episode_order(self.part)
        rows = (
            db.session.query(ChapterState, MediaPart)
            .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
            .filter(
                ChapterState.media_identity_id == self.state.media_identity_id,
                ChapterState.id != self.state.id,
                or_(
                    ChapterState.transcript_key.isnot(None),
                    ChapterState.opening_key.isnot(None),
                ),
            )
            .all()
        )
        rows.sort(
            key=lambda r: (
                abs(_episode_order(r[1])[1] - here[1])
                + 100 * abs(_episode_order(r[1])[0] - here[0])
            )
        )
        found = []
        for sibling, _part in rows[:limit]:
            segments = _transcript(sibling.transcript_key) or _transcript(
                sibling.opening_key
            )
            if segments:
                found.append(segments)
        return found


def _check_recap(ep: _Episode, fields) -> str:
    from . import recaps

    state = ep.state
    no_recap = ep.full() is None and _series_without_recaps(state)
    if not no_recap:
        segments = ep.span(0, recaps.OPENER_WITHIN + recaps.SEARCH_SECONDS)
        no_recap = recaps.opener(segments) is None
    if no_recap:
        # No "previously on": a recap Intro Skipper guessed is a cold open.
        state.recap_checked_at = utcnow()
        state.recap_start = state.recap_end = None
        state.recap_source = "no opener"
        try:
            _mark_segment(state, ep.part, ep.media, "Recap", clear_guesses=True)
        except IntegrationError:
            pass
        return "recap: none"
    found = _recap_finder(state, ep.part)(segments, ep.scenes(), ep.model())
    previously = state.recap_source
    _record_recap(state, ep.part, ep.media, found.as_dict() if found else None)
    if found and curatarr_wrote(state, ep.info):
        _add_recap_chapter(state, ep.part, ep.media, ep.info, found, fields)
    if found and found.source == "closing line" and previously is None:
        _recheck_siblings(state)
    return f"recap until {found.end:.0f}s" if found else "recap: unclear"


def _check_preview(ep: _Episode) -> str:
    from . import markers, recaps
    from .semantic import choose_preview_line, confirm_span

    state = ep.state
    state.preview_checked_at = utcnow()
    if ep.full() is None and _series_without(state, "preview"):
        state.preview_source = "no opener"
        return "preview: series has none"
    duration = ep.info.duration
    tail = ep.span(duration - markers.PREVIEW_SEARCH, duration)
    if markers.preview_opener(tail, duration) is None:
        state.preview_start = state.preview_end = None
        state.preview_source = "no opener"
        return "preview: none"
    model = ep.model()
    found = markers.find_preview(
        tail,
        duration,
        later=recaps.earlier_grams(ep.neighbours(later=True, count=1)),
        choose_last_line=(lambda lines: choose_preview_line(model, lines))
        if model
        else None,
        confirm=(lambda lines: confirm_span(model, lines, "preview"))
        if model
        else None,
        scenes=ep.scenes(),
    )
    if found:
        state.preview_start, state.preview_end = found.start, found.end
        state.preview_source = found.source
    else:
        state.preview_start = state.preview_end = None
        state.preview_source = "none"
    try:
        _mark_segment(state, ep.part, ep.media, "Preview")
    except IntegrationError as exc:
        _log("preview_segment_failed", state_id=state.id, error=str(exc))
    return f"preview from {found.start:.0f}s" if found else "preview: unclear"


def _check_markers(ep: _Episode) -> str:
    """Hide Intro Skipper's credits and intros that are plainly wrong."""
    from . import markers, recaps
    from .integrations import IntroSkipper

    state = ep.state
    item_id = jellyfin_item_id(ep.part, ep.media)
    if not item_id:
        return "markers: no Jellyfin item"
    plugin = IntroSkipper(client("jellyfin"))
    try:
        segments = plugin.segments(item_id)
    except IntegrationError as exc:
        return f"markers: {exc}"
    if segments is None:
        return "markers: Intro Skipper not installed"
    live = [
        s for s in segments if not s.get("Suppressed") and s.get("Source") != "User"
    ]
    credits = [s for s in live if s.get("Type") == "Credits"]
    intros = [s for s in live if s.get("Type") == "Introduction"]
    duration = ep.info.duration

    def speech_after(start, end):
        try:
            return ep.span(start, end)
        except (MediaToolError, RuntimeError):
            return None

    checked = [i for i in intros if i.get("Source") in markers.CHECKED_INTRO_SOURCES]
    others = ep.others() if checked or len(credits) > 1 else []
    other_grams = recaps.said_in(others) if others else None
    bad = markers.bad_outros(credits, duration, speech_after, other_grams)
    flagged = {id(segment) for segment, _reason in bad}
    preview = (
        [(state.preview_start, state.preview_end)]
        if state.preview_start is not None and state.preview_end is not None
        else []
    )
    trims = []
    for segment, new_start, reason in markers.talk_in_credits(
        [c for c in credits if id(c) not in flagged], speech_after, other_grams, preview
    ):
        if new_start is None:
            bad.append((segment, reason))
        else:
            trims.append((segment, new_start, reason))
    if checked and other_grams:
        lines = []
        for intro in checked:
            try:
                lines += ep.span(intro["Start"], intro["End"])
            except (MediaToolError, RuntimeError):
                pass
        bad += markers.bad_intros(checked, lines, other_grams)
    hidden = list(state.markers_hidden or [])
    for segment, reason in bad:
        try:
            plugin.hide(item_id, segment["Id"])
        except IntegrationError as exc:
            _log("marker_hide_failed", state_id=state.id, error=str(exc))
            continue
        hidden.append(
            {
                "id": segment["Id"],
                "type": segment.get("Type"),
                "start": round(segment["Start"], 1),
                "end": round(segment["End"], 1),
                "source": segment.get("Source"),
                "reason": reason,
                "at": utcnow().isoformat(),
            }
        )
        _log(
            "marker_hidden",
            state_id=state.id,
            kind=segment.get("Type"),
            start=segment["Start"],
            end=segment["End"],
            reason=reason,
        )
    for segment, new_start, reason in trims:
        try:
            ours = plugin.add(item_id, "Credits", new_start, segment["End"])
        except IntegrationError as exc:
            _log("marker_trim_failed", state_id=state.id, error=str(exc))
            continue
        try:
            plugin.hide(item_id, segment["Id"])
        except IntegrationError as exc:
            plugin.hide(item_id, ours)  # don't leave two credits markers
            _log("marker_hide_failed", state_id=state.id, error=str(exc))
            continue
        hidden.append(
            {
                "id": segment["Id"],
                "type": segment.get("Type"),
                "start": round(segment["Start"], 1),
                "end": round(segment["End"], 1),
                "source": segment.get("Source"),
                "reason": f"{reason}; credits now start there",
                "replaced_by": ours,
                "new_start": round(new_start, 1),
                "at": utcnow().isoformat(),
            }
        )
        _log("marker_trimmed", state_id=state.id, segment=segment["Id"])
    state.markers_hidden = hidden or None
    changed = [f"{len(bad)} hidden"] if bad else []
    changed += [f"{len(trims)} trimmed"] if trims else []
    return f"markers: {', '.join(changed)}" if changed else "markers: ok"


_summaries_cache: dict = {}
SUMMARIES_TTL = 1800


def _season_summaries(media, season) -> dict | None:
    """{episode number: "title. overview"} for one season, from Sonarr."""
    if not media.sonarr_id:
        return None
    cached = _summaries_cache.get(media.sonarr_id)
    if cached is None or time.monotonic() - cached[0] > SUMMARIES_TTL:
        episodes = client("sonarr").episodes(media.sonarr_id) or []
        cached = (time.monotonic(), episodes)
        _summaries_cache[media.sonarr_id] = cached
    return {
        e["episodeNumber"]: f"{e.get('title') or ''}. {e.get('overview') or ''}"
        for e in cached[1]
        if e.get("seasonNumber") == season
    }


def _check_identity(ep) -> str:
    """Flag an episode whose transcript fits another episode's summary much
    better than its own, or whose default audio isn't English."""
    from . import identity
    from .transcribe import load

    state, part = ep.state, ep.part
    full = ep.full()
    if full is None:
        return "identity: no full transcript"
    state.identity_checked_at = utcnow()
    issue = None
    hit = load(cache_dir() / "transcripts" / f"{state.transcript_key}.json")
    language = (hit[1] if hit else {}).get("language")
    foreign = identity.foreign_audio(language, ep.info.streams)
    if foreign:
        spoken, english = foreign
        issue = {
            "kind": "audio",
            "language": spoken,
            "english_track": english,
            "sample": " ".join(s.text.strip() for s in full[10:14])[:200],
        }
    else:
        try:
            summaries = _season_summaries(ep.media, part.season_number)
        except IntegrationError as exc:
            state.identity_checked_at = None  # try again next time
            return f"identity: Sonarr unavailable ({exc})"
        if summaries:
            # Recaps and previews are clips of other episodes; leave them out.
            spans = [
                (start, end)
                for start, end in (
                    (state.recap_start, state.recap_end),
                    (state.preview_start, state.preview_end),
                )
                if end is not None
            ]
            text = " ".join(
                s.text for s in full if not any(a <= s.start < b for a, b in spans)
            )
            match = identity.better_match(part.episode_number, text, summaries)
            if match is None and identity.fits_own(
                part.episode_number, text, summaries
            ):
                issue = {"kind": "confirmed"}
            if match:
                other, margin, certain = match
                middle = len(full) // 2
                issue = {
                    "kind": "episode",
                    "file": part.file_path,
                    "season": part.season_number,
                    "filed_as": part.episode_number,
                    "fits": other,
                    "margin": margin,
                    "certain": certain,
                    "own_summary": summaries.get(part.episode_number, "")[:400],
                    "other_summary": summaries.get(other, "")[:400],
                    "sample": " ".join(
                        s.text.strip() for s in full[middle : middle + 6]
                    )[:300],
                }
    state.identity_issue = issue
    if issue and issue["kind"] != "confirmed":
        _log(
            "identity_flagged",
            state_id=state.id,
            kind=issue["kind"],
            detail=issue.get("language") or issue.get("fits"),
        )
    if issue and issue["kind"] == "confirmed":
        return "identity: confirmed"
    return f"identity: {issue['kind']} flagged" if issue else "identity: ok"


def identity_check_enabled() -> bool:
    """Whether episodes are checked against their summaries (off by default)."""
    try:
        return bool(options()["episode_identity_check"])
    except ChapterConfigError:
        return False


def _needs_marker_check():
    """Episodes a skip-marker check still has work for."""
    if not identity_check_enabled():
        return ChapterState.markers_checked_at.is_(None)
    return or_(
        ChapterState.markers_checked_at.is_(None),
        and_(
            ChapterState.identity_checked_at.is_(None),
            ChapterState.transcript_key.isnot(None),
        ),
    )


def identity_issues(library_id=None):
    if not identity_check_enabled():
        return []
    # Through the file's own part: a long-lived state's media_identity_id can
    # point at an identity discovery has since replaced.
    query = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(ChapterState.identity_issue.isnot(None))
        .order_by(ChapterState.identity_checked_at.desc())
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return [
        s
        for s in query
        # A cleared flag can be stored as JSON null, which passes "IS NOT NULL".
        if s.identity_issue
        and not s.identity_issue.get("dismissed")
        and (s.identity_issue or {}).get("kind") != "confirmed"
        # A flag on a file that's since been renamed or replaced is stale. Names,
        # not paths: discovery and Sonarr write the same file's path differently.
        and _same_file((s.identity_issue or {}).get("file"), s.part.file_path)
    ]


def _same_file(flagged, current) -> bool:
    if not flagged or not current:
        return True
    return os.path.basename(str(flagged).replace("\\", "/")) == os.path.basename(
        str(current).replace("\\", "/")
    )


def identity_flags_api() -> list[dict]:
    """Wrong-episode flags for other tools (e.g. one that offers the fix in Sonarr).

    Says whether Curatarr has checked the episode the file fits: if that one's
    own file is confirmed, the flagged file is a duplicate; if it's flagged as
    fitting this one, the two were swapped.
    """
    flags = []
    for state in identity_issues():
        issue = state.identity_issue
        if issue.get("kind") != "episode":
            continue
        part = state.part
        media = part.media
        target = (
            db.session.query(ChapterState)
            .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
            .filter(
                MediaPart.media_identity_id == media.id,
                MediaPart.season_number == issue["season"],
                MediaPart.episode_number == issue["fits"],
                MediaPart.has_file.is_(True),
            )
            .first()
        )
        target_issue = (target.identity_issue or {}) if target else {}
        flags.append(
            {
                "series_title": media.title,
                "sonarr_series_id": media.sonarr_id,
                "season": issue["season"],
                "filed_as": issue["filed_as"],
                "fits": issue["fits"],
                "margin": issue["margin"],
                # Flags from before this was recorded count as uncertain.
                "certain": bool(issue.get("certain")),
                "file": part.file_path,
                "sample": issue.get("sample"),
                "own_summary": issue.get("own_summary"),
                "other_summary": issue.get("other_summary"),
                # Only a positive match counts: "not flagged" isn't proof (Wonder
                # Woman S1 is shifted by one, and each file only nearly fits).
                "target_confirmed": target_issue.get("kind") == "confirmed",
                "swapped": target_issue.get("kind") == "episode"
                and target_issue.get("fits") == issue["filed_as"]
                and not target_issue.get("dismissed"),
            }
        )
    return flags


def dismiss_identity(state):
    state.identity_issue = {**(state.identity_issue or {}), "dismissed": True}
    audit(
        "identity_dismissed",
        state.media_identity_id,
        f"Dismissed a possible wrong-episode flag on {_part_label(state.part)}.",
        f"identity-dismiss:{state.id}:{utcnow().isoformat()}",
    )
    db.session.commit()


MOVIE_HIDDEN_TYPES = ("Introduction", "Recap", "Preview")


def _check_movie(state, part, media) -> str:
    """Movies keep only their credits marker: Intro Skipper's movie "recaps" and
    "intros" come from chapter names like "Chapter 1" or "Opening Credits"."""
    from .integrations import IntroSkipper

    item_id = jellyfin_item_id(part, media)
    if not item_id:
        return "movie: no Jellyfin item"
    plugin = IntroSkipper(client("jellyfin"))
    segments = plugin.segments(item_id)
    if segments is None:
        return "movie: Intro Skipper not installed"
    hidden = list(state.markers_hidden or [])
    count = 0
    for segment in segments:
        if (
            segment.get("Type") not in MOVIE_HIDDEN_TYPES
            or segment.get("Suppressed")
            or segment.get("Source") == "User"
        ):
            continue
        plugin.hide(item_id, segment["Id"])
        count += 1
        hidden.append(
            {
                "id": segment["Id"],
                "type": segment["Type"],
                "start": round(segment.get("Start", 0), 1),
                "end": round(segment.get("End", 0), 1),
                "source": segment.get("Source"),
                "reason": "movies keep only their credits marker",
                "at": utcnow().isoformat(),
            }
        )
    state.markers_hidden = hidden or None
    return f"movie: {count} hidden" if count else "movie: ok"


def run_recap(state, part, media, info, config, opts, fields, *, llm=None):
    """Check an episode's skip markers: its recap, its "next time on" preview,
    and Intro Skipper's own intro and credits markers.

    Transcribes only what's needed when there's no saved transcript (the
    opening, the end, or a minute or two after a suspect credits marker).
    """
    results = []
    if part.kind != "episode":
        state.recap_checked_at = state.preview_checked_at = utcnow()
        state.recap_source = state.preview_source = "no opener"
        state.markers_checked_at = utcnow()
        if opts["movie_credits_only"]:
            try:
                results.append(_check_movie(state, part, media))
            except IntegrationError as exc:
                results.append(f"failed: {exc}")
        else:
            results.append("movie: left alone")
    elif not _smart(state):
        state.recap_checked_at = state.preview_checked_at = utcnow()
        state.markers_checked_at = utcnow()
        state.recap_source = state.preview_source = "no opener"
        results.append("not applicable")
    else:
        ep = _Episode(state, part, media, info, config, opts, llm)
        for check in (
            (lambda: _check_recap(ep, fields))
            if state.recap_checked_at is None
            else None,
            (lambda: _check_preview(ep)) if state.preview_checked_at is None else None,
            (lambda: _check_markers(ep)) if state.markers_checked_at is None else None,
            (lambda: _check_identity(ep))
            if state.identity_checked_at is None and identity_check_enabled()
            else None,
        ):
            if check is None:
                continue
            try:
                results.append(check())
            except (MediaToolError, RuntimeError) as exc:
                results.append(f"failed: {exc}")
                state.chapter_error = f"Skip-marker check failed: {exc}"
        state.markers_checked_at = utcnow()
    state.job_state, state.requested_action = "idle", None
    state.next_attempt_at = None
    db.session.commit()
    _log("recap_checked", result="; ".join(results), **fields)
    return state.chapter_status


def _recheck_siblings(state):
    """Once a series' closing line is known, recheck episodes that missed it."""
    for sibling in db.session.query(ChapterState).filter(
        ChapterState.media_identity_id == state.media_identity_id,
        ChapterState.recap_source == "none",
        ChapterState.job_state == "idle",
    ):
        sibling.job_state, sibling.requested_action = "queued", "recap"
        sibling.requested_at = utcnow()
        sibling.recap_checked_at = None  # only the recap is looked at again


def _add_recap_chapter(state, part, media, info, recap, fields):
    """Put "Previously On" in front of Curatarr's chapters, or move its end."""
    from .chapters import RECAP_CHAPTER_NAME

    current = [Chapter(c["start"], c["name"]) for c in info.chapters]
    if not current:
        return
    if current[0].name == RECAP_CHAPTER_NAME:
        rest = current[1:]
    else:
        rest = [Chapter(recap.end, current[0].name), *current[1:]]
    rest = [rest[0], *(c for c in rest[1:] if c.start > recap.end + 30)]
    rest[0] = Chapter(recap.end, rest[0].name)
    chapters = [Chapter(0.0, RECAP_CHAPTER_NAME), *rest]
    if fingerprint([c.as_dict() for c in chapters]) == fingerprint(info.chapters):
        return
    try:
        validate_chapters(chapters, info.duration)
        write_chapters(info.path, chapters)
        after = probe(info.path)
    except (MediaToolError, InvalidChapters, OSError) as exc:
        state.chapter_error = f"Adding the recap chapter failed: {exc}"
        return
    if fingerprint(after.chapters) != fingerprint([c.as_dict() for c in chapters]):
        state.chapter_error = (
            "Adding the recap chapter failed: it read back differently"
        )
        return
    state.written_fingerprint = fingerprint(after.chapters)
    state.written_chapters_json = after.chapters
    state.chapter_count = len(chapters)
    state.chapter_file_size, state.chapter_file_mtime = after.size, after.mtime
    _refresh_jellyfin(part, media)
    _log("recap_chapter_written", end=recap.end, **fields)


def queue_recaps(library_id=None) -> int:
    """Check every episode not yet checked for a recap, in episode order."""
    query = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            MediaPart.kind == "episode",
            MediaPart.has_file.is_(True),
            _needs_marker_check(),
            ChapterState.job_state == "idle",
            MediaIdentity.library_id.in_(_smart_library_ids() or {""}),
        )
        .order_by(
            MediaIdentity.title, MediaPart.season_number, MediaPart.episode_number
        )
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    ids = [row.id for row in query.with_entities(ChapterState.id)]
    db.session.rollback()  # end the read before writing
    now = utcnow()
    queued = 0
    # In batches, each committed at once: one long write of thousands of rows
    # held SQLite's lock until other writers gave up ("database is locked").
    for offset in range(0, len(ids), QUEUE_BATCH):
        for position, state_id in enumerate(
            ids[offset : offset + QUEUE_BATCH], start=offset
        ):
            queued += db.session.execute(
                update(ChapterState)
                .where(ChapterState.id == state_id, ChapterState.job_state == "idle")
                .values(
                    job_state="queued",
                    requested_action="recap",
                    # Episode order: earlier episodes teach later ones the closing line.
                    requested_at=now + timedelta(milliseconds=position),
                )
            ).rowcount
        db.session.commit()
    return queued


QUEUE_BATCH = 500


def request_skip_marker_check(library_id=None):
    """Ask the worker to queue skip-marker checks (the page doesn't wait for it)."""
    set_setting("skip_markers_requested", {"library_id": library_id})


def run_requested_skip_marker_check() -> int:
    requested = setting("skip_markers_requested")
    if not requested:
        return 0
    set_setting("skip_markers_requested", None)
    count = queue_recaps(requested.get("library_id"))
    _log("skip_marker_checks_queued", count=count)
    return count


def recaps_to_check(library_id=None, *, unqueued=False) -> int:
    """Episodes not yet checked for a recap (``unqueued``: and not waiting to be)."""
    query = (
        db.session.query(func.count(ChapterState.id))
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            MediaPart.kind == "episode",
            MediaPart.has_file.is_(True),
            _needs_marker_check(),
            MediaIdentity.library_id.in_(_smart_library_ids() or {""}),
        )
    )
    if unqueued:
        query = query.filter(ChapterState.job_state == "idle")
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return query.scalar()


def markers_checked(library_id=None) -> int:
    """Episodes a skip-marker check has finished (the other half of recaps_to_check)."""
    query = (
        db.session.query(func.count(ChapterState.id))
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            MediaPart.kind == "episode",
            MediaPart.has_file.is_(True),
            ChapterState.markers_checked_at.isnot(None),
            MediaIdentity.library_id.in_(_smart_library_ids() or {""}),
        )
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return query.scalar()


def previews_found(library_id=None) -> int:
    query = (
        db.session.query(func.count(ChapterState.id))
        .join(MediaIdentity, ChapterState.media_identity_id == MediaIdentity.id)
        .filter(ChapterState.preview_end.isnot(None))
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return query.scalar()


def markers_hidden(library_id=None) -> int:
    query = (
        db.session.query(ChapterState.markers_hidden)
        .join(MediaIdentity, ChapterState.media_identity_id == MediaIdentity.id)
        .filter(ChapterState.markers_hidden.isnot(None))
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return sum(len(hidden or []) for (hidden,) in query)


def recaps_found(library_id=None) -> int:
    query = (
        db.session.query(func.count(ChapterState.id))
        .join(MediaIdentity, ChapterState.media_identity_id == MediaIdentity.id)
        .filter(ChapterState.recap_end.isnot(None))
    )
    if library_id:
        query = query.filter(MediaIdentity.library_id == library_id)
    return query.scalar()


# --- Reviewing skip markers ----------------------------------------------------


def marker_review(kind=None, limit=200) -> list[dict]:
    """Recaps and previews Curatarr marked and markers it hid, newest first."""
    query = (
        db.session.query(ChapterState)
        .filter(
            or_(
                ChapterState.recap_end.isnot(None),
                ChapterState.preview_end.isnot(None),
                ChapterState.markers_hidden.isnot(None),
            )
        )
        .order_by(ChapterState.markers_checked_at.desc().nullslast())
    )
    rows = []
    for state in query.limit(limit * 3):
        when = state.markers_checked_at or state.recap_checked_at
        if state.recap_end is not None and kind in (None, "recap"):
            rows.append(
                {
                    "state": state,
                    "kind": "recap",
                    "when": when,
                    "start": state.recap_start,
                    "end": state.recap_end,
                    "why": f"from the {state.recap_source}",
                }
            )
        if state.preview_end is not None and kind in (None, "preview"):
            rows.append(
                {
                    "state": state,
                    "kind": "preview",
                    "when": when,
                    "start": state.preview_start,
                    "end": state.preview_end,
                    "why": state.preview_source,
                }
            )
        if kind in (None, "hidden"):
            for hidden in state.markers_hidden or []:
                rows.append(
                    {
                        "state": state,
                        "kind": "hidden",
                        "when": when,
                        "start": hidden["start"],
                        "end": hidden["end"],
                        "why": hidden["reason"],
                        "segment": hidden,
                    }
                )
    return rows[:limit]


def undo_marker(state: ChapterState, kind: str, segment_id: str | None = None) -> str:
    """Undo one skip-marker change: drop a recap or preview Curatarr marked (and
    its "Previously On" chapter), or restore an Intro Skipper marker it hid.

    An undone recap or preview isn't marked again unless the episode is checked
    again from its page.
    """
    from .integrations import IntroSkipper

    part, media = state.part, state.media
    item_id = jellyfin_item_id(part, media)
    plugin = IntroSkipper(client("jellyfin"))
    if kind == "hidden":
        hidden = [h for h in state.markers_hidden or [] if h["id"] == segment_id]
        if not hidden:
            raise ValueError("That marker isn't one Curatarr hid")
        if item_id:
            if hidden[0].get("replaced_by"):
                plugin.hide(item_id, hidden[0]["replaced_by"])  # the trimmed copy
            plugin.restore(item_id, segment_id)
        state.markers_hidden = [
            h for h in state.markers_hidden if h["id"] != segment_id
        ] or None
        message = f"Restored Intro Skipper's {hidden[0]['type'].lower()} marker."
    elif kind in ("recap", "preview"):
        field = kind
        ours = getattr(state, f"{field}_segment_id")
        if item_id and ours:
            plugin.hide(item_id, ours)
        setattr(state, f"{field}_start", None)
        setattr(state, f"{field}_end", None)
        setattr(state, f"{field}_segment_id", None)
        setattr(state, f"{field}_source", "undone")
        message = f"Removed the {kind}."
        if kind == "recap":
            message += _remove_recap_chapter(state, part, media)
    else:
        raise ValueError(f"Unknown marker kind: {kind}")
    audit(
        "skip_marker_undone",
        media.id,
        f"{message} ({_part_label(part)})",
        f"skip-marker-undo:{state.id}:{kind}:{segment_id or ''}:{utcnow().isoformat()}",
        payload={"kind": kind, "segment": segment_id},
    )
    db.session.commit()
    return message


def _remove_recap_chapter(state, part, media) -> str:
    """Take "Previously On" back out of Curatarr's chapters, if it's there."""
    from .chapters import RECAP_CHAPTER_NAME

    path = local_path(part.file_path)
    if not path or not file_exists(path):
        return ""
    try:
        info = probe(path)
    except (MediaToolError, OSError):
        return " The chapters couldn't be read, so they're unchanged."
    chapters = [Chapter(c["start"], c["name"]) for c in info.chapters]
    if not chapters or chapters[0].name != RECAP_CHAPTER_NAME:
        return ""
    if not curatarr_wrote(state, info) or len(chapters) < 2:
        return " The file's chapters changed since Curatarr wrote them, so they're left alone."
    chapters = [Chapter(0.0, chapters[1].name), *chapters[2:]]
    try:
        write_chapters(info.path, chapters)
        after = probe(info.path)
    except (MediaToolError, OSError) as exc:
        return f" Removing the chapter failed: {exc}"
    state.written_fingerprint = fingerprint(after.chapters)
    state.written_chapters_json = after.chapters
    state.chapter_count = len(after.chapters)
    state.chapter_file_size, state.chapter_file_mtime = after.size, after.mtime
    _refresh_jellyfin(part, media)
    return ' The "Previously On" chapter is gone too.'


MOVIE_SWEEP = 25  # movies checked per worker cycle (a few API calls each)


def check_movie_markers(limit=MOVIE_SWEEP) -> int:
    """Hide Intro Skipper's recaps, previews and intros on movies not yet checked.

    Runs from the worker cycle, outside the job queue: it needs no transcript
    and must not hold up a movie's chapter job.
    """
    try:
        if not options()["movie_credits_only"]:
            return 0
    except ChapterConfigError:
        return 0
    parts = (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.kind == "movie_file",
            MediaPart.has_file.is_(True),
            ChapterState.markers_checked_at.is_(None),
        )
        .limit(limit * 2)
        .all()
    )
    checked = 0
    for part in parts:
        if not part.media.library or not part.media.library.managed:
            continue
        state = state_for(part)
        try:
            result = _check_movie(state, part, part.media)
        except IntegrationError as exc:
            _log("movie_markers_failed", state_id=state.id, error=str(exc))
            break  # Jellyfin is unreachable; try again next cycle
        state.markers_checked_at = utcnow()
        db.session.commit()
        if result.endswith("hidden"):
            _log("movie_markers_checked", state_id=state.id, result=result)
        checked += 1
        if checked == limit:
            break
    return checked


def fix_default_audio(state) -> str:
    """Make the file's English audio track the default, in place.

    Chapters and subtitles Curatarr made came from the foreign track, so
    they're queued to be redone from the English one.
    """
    from .mediafile import english_audio, set_default_audio

    part, media = state.part, state.media
    path = local_path(part.file_path)
    if not path or not file_exists(path):
        raise ValueError("The file wasn't found")
    info = probe(path)
    writable, reason = can_write(info)
    if not writable:
        raise ValueError(reason)
    english = english_audio(info)
    if english is None:
        raise ValueError("This file has no English audio track")
    set_default_audio(path, info, english)
    after = probe(path)
    default = [
        s["index"]
        for s in after.streams
        if s.get("codec_type") == "audio" and s.get("default")
    ]
    if default != [english]:
        raise ValueError(
            "The change didn't read back as expected; the file may need checking"
        )
    ours = curatarr_wrote(state, after)
    state.chapter_file_size, state.chapter_file_mtime = after.size, after.mtime
    state.identity_issue = {
        **(state.identity_issue or {}),
        "fixed": True,
        "dismissed": True,
    }
    state.identity_checked_at = None  # checked again with the English transcript
    _refresh_jellyfin(part, media)
    audit(
        "default_audio_fixed",
        media.id,
        f"Made the English audio track the default on {_part_label(part)}.",
        f"default-audio:{state.id}:{utcnow().isoformat()}",
        payload={"file": path, "stream": english},
    )
    message = "English is now the default audio track."
    if ours and state.job_state == "idle":
        if state.subtitle_fingerprint:
            state.subtitle_status = None  # redone after the chapters
        db.session.commit()
        request(part, "regenerate")
        message += (
            " Curatarr's chapters (and subtitles) are queued to be redone from it."
        )
    db.session.commit()
    return message


# --- Searching what was said ----------------------------------------------------


def search_transcripts(query: str, limit: int = 10) -> list[dict]:
    """Episodes and films whose transcript has the words, best match first."""
    from .search import index

    rows = (
        db.session.query(ChapterState.id, ChapterState.transcript_key)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(ChapterState.transcript_key.isnot(None), MediaPart.has_file.is_(True))
        .all()
    )
    index.refresh(rows, _transcript)
    results = []
    for hit in index.search(query, limit):
        state = db.session.get(ChapterState, hit["key"])
        if state is None or state.part is None:
            continue
        part, media = state.part, state.part.media
        results.append(
            {
                "title": media.title,
                "season": part.season_number if part.kind == "episode" else None,
                "episode": part.episode_number if part.kind == "episode" else None,
                "label": file_label(state),
                "at": round(hit["at"], 1),
                "timestamp": timestamp(hit["at"])[:8],
                "exact": hit["exact"],
                "lines": hit["lines"],
                "part_id": part.id,
                "media_id": media.id,
                "jellyfin_id": jellyfin_item_id(part, media),
            }
        )
    return results


def transcripts_searchable() -> int:
    return (
        db.session.query(func.count(ChapterState.id))
        .filter(ChapterState.transcript_key.isnot(None))
        .scalar()
    )


LINK_SWEEP = 20  # episodes looked at per worker cycle (one probe each)
_link_tried: set[str] = set()


def link_transcripts(limit=LINK_SWEEP) -> int:
    """Link transcripts made before Curatarr recorded which file they belong to.

    Search and the identity check find transcripts through transcript_key, which
    only newer jobs record. Smart Chapters files have a transcript on disk under
    a key worked out from the file itself, so probing the file finds it. Files
    without one are only tried once per worker run.
    """
    from .transcribe import cache_key

    rows = (
        db.session.query(ChapterState)
        .filter(
            ChapterState.transcript_key.is_(None),
            ChapterState.chapter_status == GENERATED_STATUSES["semantic"],
            ChapterState.id.notin_(list(_link_tried) or [""]),
        )
        .limit(limit)
        .all()
    )
    # Read everything first, then write once: holding SQLite's write lock while
    # probing files on a network share made a Jellyfin webhook time out.
    work = []
    for state in rows:
        _link_tried.add(state.id)
        part = state.part
        path = local_path(part.file_path) if part else None
        if path:
            work.append((state.id, path, part.media.library_id))
    db.session.rollback()  # end the read transaction before the slow part
    found = {}
    for state_id, path, library_id in work:
        if not file_exists(path):
            continue
        try:
            config = config_for(library_id)
            info = probe(path)
        except (ChapterConfigError, MediaToolError, OSError):
            continue
        key = cache_key(info, config["semantic"]["model"])
        if (cache_dir() / "transcripts" / f"{key}.json").exists():
            found[state_id] = key
    for state_id, key in found.items():
        state = db.session.get(ChapterState, state_id)
        if state is not None and state.transcript_key is None:
            state.transcript_key = key
    db.session.commit()
    linked = len(found)
    if linked:
        _log("transcripts_linked", count=linked)
    return linked
