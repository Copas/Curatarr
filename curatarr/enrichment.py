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
from sqlalchemy import func

from . import db, leases
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
from .services import _as_datetime, audit, client, set_setting, setting

log = logging.getLogger("curatarr.chapters")

WRITE_ACTIONS = {"auto", "generate", "regenerate", "remove"}
ACTIONS = WRITE_ACTIONS | {"preview", "scan"}
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
):
    """Plan chapters, falling back Smart → scene-aligned → fixed intervals.

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

            segments, details = transcribe(
                info,
                config["semantic"]["model"],
                opts["whisper_device"],
                cache_dir() / "transcripts",
            )
            result["transcript"] = {
                **details,
                "segments": len(segments),
                "words": sum(len(s.text.split()) for s in segments),
                "sample": [[round(s.start, 1), s.text.strip()] for s in segments[:12]],
            }
            model = llm or _language_model(config, opts)
            plan = semantic_plan(
                info.duration,
                scenes or [],
                segments,
                config,
                lambda window, candidates, excerpt: choose_boundary(
                    model, window, candidates, excerpt
                ),
                lambda excerpts: name_chapters(model, excerpts, context),
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

    analysis = analyse(
        info,
        config,
        opts,
        every_method=action == "preview",
        llm=llm,
        context=_context(part, media),
    )
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


def _due_jobs(limit, *, scans):
    now = utcnow()
    query = db.session.query(ChapterState).filter(ChapterState.job_state == "queued")
    if scans:
        query = query.filter(ChapterState.requested_action == "scan")
    else:
        query = query.filter(ChapterState.requested_action != "scan")
    rows = query.order_by(ChapterState.requested_at).limit(limit * 4).all()
    return [
        row
        for row in rows
        if not row.next_attempt_at or _as_datetime(row.next_attempt_at) <= now
    ][:limit]


def _run_leased(app, state_ids, llm=None):
    with app.app_context():
        for state_id in state_ids:
            scope = f"chapters:{state_id}"
            owner = leases.acquire(scope, LEASE_SECONDS)
            if not owner:
                continue
            try:
                state = db.session.get(ChapterState, state_id)
                if not state or state.job_state != "queued":
                    continue
                state.job_state = "running"
                db.session.commit()
                with leases.keep_alive(scope, owner, LEASE_SECONDS):
                    run_job(state_id, llm=llm)
            except Exception:
                db.session.rollback()
                app.logger.exception("Chapter job %s failed unexpectedly", state_id)
                state = db.session.get(ChapterState, state_id)
                if state and state.job_state == "running":
                    _fail(state, "Unexpected error; see the worker log")
                    db.session.commit()
            finally:
                leases.release(scope, owner)


def dispatch(*, synchronous=False, llm=None) -> int:
    """Start queued chapter jobs up to the configured concurrency.

    Scans (ffprobe only) run in one batch thread of their own so a library scan
    does not wait behind, or crowd out, transcription jobs.
    """
    recover_stale_jobs()
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
    try:
        concurrency = options()["concurrency"]
    except ChapterConfigError:
        concurrency = GLOBAL_DEFAULTS["concurrency"]
    # Counted in the database, so the limit holds across worker processes.
    busy = (
        db.session.query(func.count(ChapterState.id))
        .filter(
            ChapterState.job_state == "running",
            ChapterState.requested_action != "scan",
        )
        .scalar()
    )
    free = concurrency - max(busy, len([k for k in running if k != "scan"]))
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
    smart_libraries = set()
    for library in db.session.query(Library):
        try:
            config = config_for(library.id)
        except ChapterConfigError:
            continue
        if config["mode"] == "smart" and config["semantic"]["enabled"]:
            smart_libraries.add(library.id)
    needing_smart = (
        needs_chapters_query(library_id)
        .filter(MediaIdentity.library_id.in_(smart_libraries or {""}))
        .count()
    )
    queued = (
        db.session.query(func.count(ChapterState.id))
        .filter(ChapterState.job_state.in_(("queued", "running")))
        .scalar()
    )
    return {
        "files": parts,
        "scanned": scanned,
        "not_scanned": max(parts - scanned, 0),
        "existing": by_status.get("existing", 0),
        "generated": generated,
        "needing": needing,
        "short": by_status.get("skipped_short", 0),
        "unsupported": by_status.get("unsupported_container", 0),
        "failed": by_status.get("failed", 0),
        "removed": by_status.get("removed", 0),
        "transcription_estimate": needing_smart,
        "queued": queued,
    }


def queue_selected(state_ids, action) -> int:
    if action not in {"preview", "generate"}:
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
        "queued": bool(state and state.job_state in {"queued", "running"}),
    }


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
