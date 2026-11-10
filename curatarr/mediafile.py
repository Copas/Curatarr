"""Read and write chapter metadata without re-encoding (ffprobe, ffmpeg, MKVToolNix).

Only Matroska files are written, in place with mkvpropedit, which edits the
container's header elements and never touches the audio/video data. Other
containers would need a full remux, so they are reported as unsupported.
"""

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import throttle
from .chapters import Chapter, chapter_file_text

# Jellyfin's bundled FFmpeg is the usual copy on a Jellyfin host without a
# system FFmpeg.
TOOL_FALLBACK_DIRS = ("/usr/lib/jellyfin-ffmpeg",)
MATROSKA_FORMATS = {"matroska", "webm", "matroska,webm"}
MATROSKA_SUFFIXES = {".mkv", ".mka", ".mk3d", ".webm"}
MP4_SUFFIXES = {".mp4", ".m4v", ".mov"}


class MediaToolError(RuntimeError):
    pass


class FileChanged(MediaToolError):
    """Another change got to the file first: plan again from a fresh look."""


def tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for directory in TOOL_FALLBACK_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def required_tool(name: str) -> str:
    path = tool(name)
    if not path:
        raise MediaToolError(f"{name} is not installed")
    return path


def _run(args, timeout):
    try:
        return throttle.run(args, timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"{Path(args[0]).name} timed out") from exc
    except OSError as exc:
        raise MediaToolError(f"{Path(args[0]).name} could not run: {exc}") from exc


@dataclass
class MediaInfo:
    path: str
    size: int
    mtime: float
    duration: float
    format_name: str
    chapters: list[dict] = field(default_factory=list)
    streams: list[dict] = field(default_factory=list)

    @property
    def matroska(self) -> bool:
        names = set(self.format_name.split(","))
        return bool(names & {"matroska", "webm"}) and (
            Path(self.path).suffix.lower() in MATROSKA_SUFFIXES
        )

    @property
    def mp4(self) -> bool:
        """An MP4 (or MOV) file: chapters go in by rewriting it (rewrite_mp4)."""
        names = set(self.format_name.split(","))
        return bool(names & {"mp4", "mov"}) and Path(self.path).suffix.lower() in (
            MP4_SUFFIXES
        )

    @property
    def video_length(self) -> float | None:
        """How long the picture runs, in seconds, when the file says."""
        lengths = [
            s["length"]
            for s in self.streams
            if s.get("codec_type") == "video" and s.get("length")
        ]
        return max(lengths) if lengths else None

    @property
    def stream_signature(self) -> list[tuple]:
        """What must be identical before and after a chapter edit. In an MP4
        the chapters are themselves a data track, made new by each rewrite,
        so there only the picture, sound and subtitle tracks count."""
        if self.mp4:
            return [
                (s.get("codec_type"), s.get("codec_name"))
                for s in self.streams
                if s.get("codec_type") in {"video", "audio", "subtitle"}
            ]
        return [
            (s.get("index"), s.get("codec_type"), s.get("codec_name"))
            for s in self.streams
        ]

    def as_dict(self):
        return {
            "path": self.path,
            "size": self.size,
            "duration": round(self.duration, 3),
            "container": self.format_name,
            "chapters": self.chapters,
            "streams": len(self.streams),
        }


def _stream_length(stream) -> float | None:
    """A track's own length: ffprobe's duration, or Matroska's DURATION tag."""
    try:
        if stream.get("duration") not in (None, "N/A"):
            return round(float(stream["duration"]), 3)
        hours, minutes, seconds = (stream.get("tags") or {})["DURATION"].split(":")
        return round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 3)
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _same_length(before: MediaInfo, after: MediaInfo, slack=2.0) -> bool:
    """The picture runs as long as before. A file's own length is its longest
    track, often a dub that runs on a few seconds (once 95 s) past the film,
    so after removing that dub the picture's length is what's compared."""
    if before.video_length and after.video_length:
        return abs(after.video_length - before.video_length) <= slack
    return abs(after.duration - before.duration) <= slack


def probe(path: str) -> MediaInfo:
    stat = os.stat(path)
    result = _run(
        [
            required_tool("ffprobe"),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_chapters",
            "-show_streams",
            path,
        ],
        timeout=120,
    )
    if result.returncode != 0:
        raise MediaToolError(
            "ffprobe could not read the file: "
            + (result.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
        )
    data = json.loads(result.stdout or "{}")
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0.0
    chapters = [
        {
            "start": round(float(c.get("start_time", 0)), 3),
            "name": ((c.get("tags") or {}).get("title") or "").strip(),
        }
        for c in data.get("chapters") or []
    ]
    return MediaInfo(
        path=path,
        size=stat.st_size,
        mtime=stat.st_mtime,
        duration=duration,
        format_name=fmt.get("format_name", ""),
        chapters=chapters,
        streams=[
            {
                "index": s.get("index"),
                "codec_type": s.get("codec_type"),
                "codec_name": s.get("codec_name"),
                "default": bool((s.get("disposition") or {}).get("default")),
                "forced": bool((s.get("disposition") or {}).get("forced")),
                "language": (s.get("tags") or {}).get("language"),
                "channels": s.get("channels"),
                "title": (s.get("tags") or {}).get("title"),
                "length": _stream_length(s),
                # A commentary, or a narrator describing the picture.
                "extra": bool(
                    (s.get("disposition") or {}).get("comment")
                    or (s.get("disposition") or {}).get("visual_impaired")
                ),
            }
            for s in data.get("streams") or []
        ],
    )


SCENE_LINE = re.compile(r"pts_time:([0-9.]+)")
SCORE_LINE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def detect_scenes(path: str, threshold: float, timeout: int = 4 * 3600):
    """Scene changes as (seconds, score), sampled at 5 frames per second.

    Frames are scaled down before comparison, which keeps a full-length film to
    a few minutes. Hardware decoding is tried first; it fails when the GPU is
    full (Ollama's model loaded), so a failure is retried with software decoding.
    """

    def run(hardware: bool):
        return _run(
            [required_tool("ffmpeg"), "-hide_banner", "-nostats", "-nostdin"]
            + (["-hwaccel", "auto"] if hardware else [])
            + [
                "-i",
                path,
                "-map",
                "0:v:0",
                "-vf",
                f"fps=5,scale=320:-2,select='gt(scene,{threshold})',metadata=print:file=-",
                "-an",
                "-sn",
                "-dn",
                "-f",
                "null",
                "-",
            ],
            timeout=timeout,
        )

    from . import adaptive

    # While Jellyfin transcodes, the video decoder is its: decode on the CPU.
    result = run(hardware=not adaptive.leave_decoder)
    if result.returncode != 0 and not adaptive.leave_decoder:
        result = run(hardware=False)
    if result.returncode != 0:
        raise MediaToolError(
            "Scene detection failed: "
            + (result.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
        )
    scenes, current = [], None
    for line in result.stdout.splitlines():
        if match := SCENE_LINE.search(line):
            current = float(match.group(1))
        elif (match := SCORE_LINE.search(line)) and current is not None:
            scenes.append((round(current, 3), round(float(match.group(1)), 3)))
            current = None
    return scenes


def write_command(path: str, chapter_file: str) -> list[str]:
    return [tool("mkvpropedit") or "mkvpropedit", path, "--chapters", chapter_file]


def remove_command(path: str) -> list[str]:
    # An empty file name removes every chapter.
    return [tool("mkvpropedit") or "mkvpropedit", path, "--chapters", ""]


def can_write(info: MediaInfo) -> tuple[bool, str | None]:
    if info.mp4:
        # Rewritten with ffmpeg (copied, nothing re-encoded): rewrite_mp4.
        if not tool("ffmpeg"):
            return False, "ffmpeg is not installed"
        if not os.access(info.path, os.W_OK) or not os.access(
            os.path.dirname(info.path) or ".", os.W_OK
        ):
            return False, "Curatarr does not have permission to modify this file"
        return True, None
    if not info.matroska:
        return False, (
            f"{info.format_name or 'This'} container can't hold chapters; only "
            "Matroska (MKV) and MP4 files get them."
        )
    if not tool("mkvpropedit"):
        return False, "mkvpropedit (MKVToolNix) is not installed"
    if not os.access(info.path, os.W_OK):
        return False, "Curatarr does not have permission to modify this file"
    return True, None


# --- One change to a file at a time -------------------------------------------
# Chapters, TV-friendly audio, subtitle tracks, default audio, dubs and seek
# repair each run in their own thread (and some in the web process). Two
# rewriting one file at once wrote the same partial copy, or the second
# replaced the file with a copy made before the first's change, undoing it.
# Every change to a file holds that file's lock (across processes), and a
# change planned from an earlier look at the file is refused if the file has
# changed since.

_held = threading.local()


def _lock_folder() -> Path:
    try:
        from flask import current_app

        folder = Path(current_app.instance_path) / "locks"
    except RuntimeError:  # outside the app (tools, tests)
        folder = Path(tempfile.gettempdir()) / "curatarr-locks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@contextlib.contextmanager
def editing(path: str, info: "MediaInfo | None" = None):
    """Hold ``path``'s lock while changing it (waits for another change to
    finish). With ``info``, the file must still be the one it describes."""
    held = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = set()
    key = os.path.realpath(path)
    if key in held:  # already ours (an edit followed by a seek repair)
        yield
        return
    name = hashlib.sha256(key.encode()).hexdigest()[:32] + ".lock"
    with open(_lock_folder() / name, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        held.add(key)
        try:
            try:
                now = file_identity_of(path) if info is not None else None
            except OSError:
                now = None  # gone: the change itself reports that
            if now is not None and now != (info.size, info.mtime):
                raise FileChanged(
                    "The file changed since it was looked at; left for next time"
                )
            yield
        finally:
            held.discard(key)
            fcntl.flock(handle, fcntl.LOCK_UN)


def _exclusive(function):
    """Run a file change under ``editing``; a MediaInfo second argument is
    the look at the file the change was planned from."""

    @functools.wraps(function)
    def wrapper(path, *args, **kwargs):
        info = args[0] if args and isinstance(args[0], MediaInfo) else None
        with editing(path, info):
            return function(path, *args, **kwargs)

    return wrapper


def _mkvpropedit(args):
    result = _run(args, timeout=600)
    # mkvpropedit exits 1 for warnings with a successful edit, 2 for errors.
    if result.returncode not in (0, 1):
        raise MediaToolError(
            "mkvpropedit failed: "
            + (result.stdout.strip().splitlines() or ["unknown error"])[-1][:200]
        )
    if seek_index_hidden(args[1]):
        from . import thumbnails

        noted = thumbnails.before_rebuild(args[1])
        rebuild_layout(args[1])
        thumbnails.after_rebuild(noted)  # Jellyfin remakes its thumbnails
    return result


# --- Seek index layout -------------------------------------------------------
# Matroska's seek head lists where the seek index (Cues) and the rest are.
# When an edit doesn't fit at the start, mkvpropedit moves the seek head to the
# end and leaves one at the start that only points there. That's valid, but
# Roku players don't follow it: without the index they can't seek, and play
# from the beginning instead. Rebuilding the file (mkvmerge, a copy, never a
# re-encode) puts a full seek head back at the start.

SEGMENT_ID = 0x18538067
SEEK_HEAD_ID = 0x114D9B74
SEEK_ID = 0x4DBB
SEEK_ID_ID = 0x53AB
VOID_ID = 0xEC
CUES_ID = 0x1C53BB6B
EBML_ID = 0x1A45DFA3


def _element_id(data, i):
    first = data[i]
    length = next((n for n in range(1, 5) if first & (0x80 >> (n - 1))), None)
    if length is None:
        raise ValueError("bad element id")
    return int.from_bytes(data[i : i + length], "big"), i + length


def _element_size(data, i):
    first = data[i]
    length = next((n for n in range(1, 9) if first & (0x80 >> (n - 1))), None)
    if length is None:
        raise ValueError("bad element size")
    value = first & (0xFF >> length)
    for byte in data[i + 1 : i + length]:
        value = (value << 8) | byte
    return value, i + length


def front_seek_ids(path: str) -> set[int] | None:
    """Element ids the first seek head points to, or None when there's none
    (or the file isn't Matroska)."""
    with open(path, "rb") as handle:
        data = handle.read(256 * 1024)
    try:
        element, i = _element_id(data, 0)
        if element != EBML_ID:
            return None
        size, i = _element_size(data, i)
        i += size
        element, i = _element_id(data, i)
        if element != SEGMENT_ID:
            return None
        _size, i = _element_size(data, i)
        while i < len(data):
            element, i = _element_id(data, i)
            size, i = _element_size(data, i)
            if element == VOID_ID:
                i += size
                continue
            if element != SEEK_HEAD_ID:
                return None  # no seek head before the rest
            ids, end = set(), i + size
            while i < end:
                child, i = _element_id(data, i)
                child_size, i = _element_size(data, i)
                if child == SEEK_ID:
                    j, child_end = i, i + child_size
                    while j < child_end:
                        inner, j = _element_id(data, j)
                        inner_size, j = _element_size(data, j)
                        if inner == SEEK_ID_ID:
                            ids.add(int.from_bytes(data[j : j + inner_size], "big"))
                        j += inner_size
                i += child_size
            return ids
    except (ValueError, IndexError):
        return None
    return None


def file_identity_of(path: str) -> tuple[int, float]:
    stat = os.stat(path)
    return stat.st_size, stat.st_mtime


def seek_index_hidden(path: str) -> bool:
    """Whether the seek head at the start only points to another one (see above)."""
    try:
        ids = front_seek_ids(path)
    except OSError:
        return False
    return bool(ids) and CUES_ID not in ids and SEEK_HEAD_ID in ids


def _keep_track_order(path: str) -> list[str]:
    """mkvmerge's options to keep the tracks in the file's own order. Left to
    itself it puts video first, so a file with its audio first (as some web
    releases are) came out reordered and failed the check below: 201 files,
    Roku still unable to seek in them."""
    result = _run([required_tool("mkvmerge"), "-J", path], 120)
    try:
        tracks = json.loads(result.stdout or "{}").get("tracks") or []
    except ValueError:
        return []
    if result.returncode not in (0, 1) or not tracks:
        return []
    return ["--track-order", ",".join(f"0:{t['id']}" for t in tracks)]


@_exclusive
def rebuild_layout(path: str) -> MediaInfo:
    """Rewrite a Matroska file with mkvmerge so its seek head is whole again.

    Every track, chapter, tag and attachment is copied as it is. The copy is
    checked (same tracks, chapters and length, index reachable) before it
    replaces the original; on any doubt the original is left alone."""
    before = probe(path)
    target = Path(path)
    partial = target.with_name(f".{target.name}.curatarr-partial")
    try:
        result = _run(
            [
                required_tool("mkvmerge"),
                "-q",
                *_keep_track_order(path),
                "-o",
                str(partial),
                path,
            ],
            4 * 3600,
        )
        # mkvmerge exits 1 for warnings with a good file, 2 for errors.
        if result.returncode not in (0, 1):
            raise MediaToolError(
                "mkvmerge failed: "
                + (result.stdout.strip().splitlines() or ["unknown error"])[-1][:200]
            )
        after = probe(str(partial))
        same_tracks = [
            (s.get("codec_type"), s.get("codec_name")) for s in after.streams
        ] == [(s.get("codec_type"), s.get("codec_name")) for s in before.streams]
        if (
            not same_tracks
            or after.chapters != before.chapters
            or abs(after.duration - before.duration) > 1.0
            or seek_index_hidden(str(partial))
        ):
            raise MediaToolError("The rebuilt copy didn't match the original")
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return probe(path)


FFMETADATA = ";FFMETADATA1"


def mp4_title(name) -> str:
    """A chapter title as an MP4 can hold it: one line, no backslashes."""
    return " ".join(str(name).replace("\\", "/").split())


def ffmetadata(chapters, duration: float) -> str:
    """Chapters as ffmpeg's metadata file: each ends where the next starts."""
    starts = [
        (
            float(c["start"] if isinstance(c, dict) else c.start),
            c["name"] if isinstance(c, dict) else c.name,
        )
        for c in chapters
    ]
    lines = [FFMETADATA]
    for number, (start, name) in enumerate(starts):
        end = starts[number + 1][0] if number + 1 < len(starts) else duration
        # ffmpeg's reader keeps a backslash doubled and loses the chapter
        # after a trailing one, so titles go without (mp4_title).
        escaped = re.sub(r"([=;#])", r"\\\1", mp4_title(name))
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(start * 1000)}",
            f"END={round(max(end, start) * 1000)}",
            f"title={escaped}",
        ]
    return "\n".join(lines) + "\n"


@_exclusive
def rewrite_mp4(path: str, metadata: str | None) -> MediaInfo:
    """Rewrite an MP4 with these chapters (an ffmetadata text) or none, every
    track copied as it is, nothing re-encoded. The copy is checked (same
    tracks and length, the chapters asked for) before it replaces the
    original, under the same name; on any doubt the original is left alone.
    Takes as long as copying the file."""
    before = probe(path)
    target = Path(path)
    # Ends in the file's own suffix, so it's checked as the MP4 it is.
    partial = target.with_name(f".{target.stem}.curatarr-partial{target.suffix}")
    with tempfile.TemporaryDirectory(prefix="curatarr-chapters-") as folder:
        meta = Path(folder) / "chapters.txt"
        if metadata is not None:
            meta.write_text(metadata, encoding="utf-8")
        command = [required_tool("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", path]
        if metadata is not None:
            command += ["-f", "ffmetadata", "-i", str(meta)]
        command += [
            "-map", "0", "-dn", "-ignore_unknown", "-c", "copy",
            "-map_metadata", "0",
            "-map_chapters", "1" if metadata is not None else "-1",
            "-movflags", "+faststart",
            "-f", "mp4" if target.suffix.lower() != ".mov" else "mov",
            str(partial),
        ]  # fmt: skip
        if shutil.disk_usage(target.parent).free < before.size * 1.05 + 2**30:
            raise MediaToolError("Not enough free space beside the file to rewrite it")
        try:
            result = _run(command, 4 * 3600)
            if result.returncode != 0:
                raise MediaToolError(
                    "ffmpeg failed: "
                    + (result.stderr.strip().splitlines() or ["unknown error"])[-1][
                        :200
                    ]
                )
            after = probe(str(partial))

            wanted = metadata.count("[CHAPTER]") if metadata else 0
            if (
                after.stream_signature != before.stream_signature
                or abs(after.duration - before.duration) > 1.0
                or len(after.chapters) != wanted
            ):
                raise MediaToolError("The rewritten copy didn't match the original")
            try:
                shutil.copymode(path, partial)
            except OSError:
                pass  # a share that doesn't keep modes
            os.replace(partial, path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    return probe(path)


@_exclusive
def write_chapters(path: str, chapters: list[Chapter]) -> None:
    info = probe(path)
    if info.mp4:
        rewrite_mp4(path, ffmetadata(chapters, info.duration))
        return
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="curatarr-chapters-", encoding="utf-8", delete=False
    ) as handle:
        handle.write(chapter_file_text(chapters))
        chapter_file = handle.name
    try:
        _mkvpropedit(write_command(path, chapter_file))
    finally:
        os.unlink(chapter_file)


ENGLISH = {"eng", "en", "english"}


def english_audio(info: MediaInfo) -> int | None:
    """Stream index of the first English audio track, or None."""
    return next(
        (
            s["index"]
            for s in info.streams
            if s.get("codec_type") == "audio"
            and str(s.get("language") or "").lower() in ENGLISH
        ),
        None,
    )


@_exclusive
def set_default_audio(path: str, info: MediaInfo, stream_index: int) -> None:
    """Make one audio track the default (and no other), in place, no re-encode."""
    audio = [s for s in info.streams if s.get("codec_type") == "audio"]
    if stream_index not in {s["index"] for s in audio}:
        raise MediaToolError("That isn't an audio track in this file")
    args = [required_tool("mkvpropedit"), path]
    for number, stream in enumerate(audio, start=1):
        flag = "1" if stream["index"] == stream_index else "0"
        args += ["--edit", f"track:a{number}", "--set", f"flag-default={flag}"]
    _mkvpropedit(args)


@_exclusive
def set_audio_language(
    path: str, info: MediaInfo, stream_index: int, language: str, ietf: str
) -> None:
    """Relabel one audio track's language (ISO 639-2 and IETF), in place."""
    audio = [s["index"] for s in info.streams if s.get("codec_type") == "audio"]
    if stream_index not in audio:
        raise MediaToolError("That isn't an audio track in this file")
    number = audio.index(stream_index) + 1
    _mkvpropedit(
        [
            required_tool("mkvpropedit"),
            path,
            "--edit",
            f"track:a{number}",
            "--set",
            f"language={language}",
            "--set",
            f"language-ietf={ietf}",
        ]
    )


@_exclusive
def remove_tracks(
    path: str, info: MediaInfo, drop: list[int], default_audio: int, *, first=False
) -> MediaInfo:
    """Rewrite the file without some tracks (a copy, never a re-encode).
    ``first``: the default audio track also becomes the first audio track.

    The copy is written beside the file, checked (same length, chapters and
    video, the expected audio and subtitles) and only then moved over it.
    """
    merge = required_tool("mkvmerge")
    listing = _run([merge, "-J", path], timeout=120)
    if listing.returncode not in (0, 1):
        raise MediaToolError("mkvmerge could not read the file")
    tracks = json.loads(listing.stdout or "{}").get("tracks") or []
    # ffprobe's stream indexes and mkvmerge's track ids follow the same order;
    # match them per type by position rather than assume the numbers agree.
    ids = {}
    for kind, probe_kind in (("audio", "audio"), ("subtitles", "subtitle")):
        ours = [s["index"] for s in info.streams if s.get("codec_type") == probe_kind]
        theirs = [t["id"] for t in tracks if t.get("type") == kind]
        if len(ours) != len(theirs):
            raise MediaToolError("The file's tracks didn't match up; left unchanged")
        ids.update(zip(ours, theirs, strict=True))
    audio = [s["index"] for s in info.streams if s.get("codec_type") == "audio"]
    subtitles = [s["index"] for s in info.streams if s.get("codec_type") == "subtitle"]
    keep_audio = [i for i in audio if i not in drop]
    keep_subtitles = [i for i in subtitles if i not in drop]
    if default_audio not in keep_audio:
        raise MediaToolError("That would remove the English audio; left unchanged")
    folder = os.path.dirname(path)
    free = shutil.disk_usage(folder).free
    if free < info.size * 1.1:
        raise MediaToolError("Not enough free space beside the file to rewrite it")
    target = os.path.join(folder, f".{os.path.basename(path)}.curatarr-partial")
    args = [
        merge,
        "-o",
        target,
        "--audio-tracks",
        ",".join(str(ids[i]) for i in keep_audio),
    ]
    if keep_subtitles:
        args += ["--subtitle-tracks", ",".join(str(ids[i]) for i in keep_subtitles)]
    else:
        args += ["--no-subtitles"]
    for index in keep_audio:
        args += [
            "--default-track-flag",
            f"{ids[index]}:{'1' if index == default_audio else '0'}",
        ]
    if first:
        # Everything keeps its place; the track takes the first audio track's.
        moved, gone = ids[default_audio], {ids[i] for i in drop}
        kept = [t for t in tracks if t["id"] not in gone]
        position = next(n for n, t in enumerate(kept) if t.get("type") == "audio")
        order = [t["id"] for t in kept if t["id"] != moved]
        order.insert(position, moved)
        args += ["--track-order", ",".join(f"0:{track}" for track in order)]
    args.append(path)
    wanted = next(s for s in info.streams if s["index"] == default_audio)
    try:
        result = throttle.run(args, timeout=4 * 3600)
        if result.returncode not in (0, 1) or not os.path.exists(target):
            raise MediaToolError(
                "mkvmerge failed: "
                + (result.stdout.strip().splitlines() or ["unknown error"])[-1][:200]
            )
        after = probe(target)
        count = lambda i, kind: sum(1 for s in i.streams if s.get("codec_type") == kind)
        lead_after = next(
            (s for s in after.streams if s.get("codec_type") == "audio"), {}
        )
        if (
            not _same_length(info, after)
            or len(after.chapters) != len(info.chapters)
            or count(after, "video") != count(info, "video")
            or count(after, "audio") != len(keep_audio)
            or count(after, "subtitle") != len(keep_subtitles)
            or (
                first
                and (
                    not lead_after.get("default")
                    or lead_after.get("language") != wanted.get("language")
                    or lead_after.get("codec_name") != wanted.get("codec_name")
                )
            )
        ):
            raise MediaToolError("The rewritten file didn't check out; left unchanged")
        os.replace(target, path)
    finally:
        if os.path.exists(target):
            os.unlink(target)
    return probe(path)


# Audio many TVs and streaming sticks (Roku, older smart TVs) can't play, so
# Jellyfin converts it during playback, which some clients handle badly.
HARD_AUDIO = {"dts", "truehd", "mlp", "flac", "pcm_s16le", "pcm_s24le", "pcm_bluray"}
EASY_AUDIO = {"ac3", "eac3", "aac"}
# What web browsers play as it is (AC3, E-AC3 and DTS they can't).
BROWSER_AUDIO = {"aac", "mp3", "opus", "flac", "vorbis"}


def compatible_audio_source(info: MediaInfo) -> int | None:
    """The track to add a TV-friendly copy of: the default audio track when
    it's hard to play and no easy track in its language exists."""
    audio = [s for s in info.streams if s.get("codec_type") == "audio"]
    if not audio:
        return None
    main = next((s for s in audio if s.get("default")), audio[0])
    if main.get("codec_name") not in HARD_AUDIO:
        return None
    language = main.get("language")
    if any(
        s.get("codec_name") in EASY_AUDIO and s.get("language") == language
        for s in audio
    ):
        return None
    return main["index"]


def browser_audio_source(info: MediaInfo) -> int | None:
    """The track to add a stereo AAC copy of: the default audio track when no
    track in its language is one web browsers can play."""
    audio = [s for s in info.streams if s.get("codec_type") == "audio"]
    if not audio:
        return None
    main = next((s for s in audio if s.get("default")), audio[0])
    language = main.get("language")
    if any(
        s.get("codec_name") in BROWSER_AUDIO and s.get("language") == language
        for s in audio
    ):
        return None
    return main["index"]


@_exclusive
def add_compatible_audio(
    path: str, info: MediaInfo, source: int, *, for_browsers=False
) -> MediaInfo:
    """Add an AC3 copy of one audio track, first and default (5.1 at most); or,
    ``for_browsers``, a stereo AAC copy, last and not default, so TVs keep the
    track they play now (the Jellyfin plugin points browsers at this one).

    Everything else is copied untouched, the original track included; only
    the new track is encoded. The copy is written beside the file, checked
    (same length, chapters, video and subtitles, one more audio track) and
    only then moved over it.
    """
    ffmpeg = required_tool("ffmpeg")
    stream = next(s for s in info.streams if s["index"] == source)
    folder = os.path.dirname(path)
    if shutil.disk_usage(folder).free < info.size * 1.1:
        raise MediaToolError("Not enough free space beside the file to rewrite it")
    target = os.path.join(folder, f".{os.path.basename(path)}.curatarr-partial")
    others = [s["index"] for s in info.streams if s.get("codec_type") == "audio"]
    args = [ffmpeg, "-v", "error", "-y", "-i", path, "-map", "0:v?"]
    if for_browsers:
        # The originals keep their order and default; the new track goes last.
        for index in others:
            args += ["-map", f"0:{index}"]
        args += ["-map", f"0:{source}"]
        new, codec = len(others), "aac"
        args += ["-map", "0:s?", "-map", "0:t?", "-map_chapters", "0", "-c", "copy"]
        args += [f"-c:a:{new}", "aac", f"-b:a:{new}", "160k", f"-ac:a:{new}", "2"]
        args += [f"-disposition:a:{new}", "0"]
        title = "AAC stereo"
    else:
        channels = min(stream.get("channels") or 6, 6)
        args += ["-map", f"0:{source}"]
        for index in others:
            args += ["-map", f"0:{index}"]
        new, codec = 0, "ac3"
        args += ["-map", "0:s?", "-map", "0:t?", "-map_chapters", "0", "-c", "copy"]
        args += ["-c:a:0", "ac3", "-b:a:0", "640k" if channels > 2 else "224k"]
        args += ["-ac:a:0", str(channels), "-disposition:a:0", "default"]
        for n in range(1, len(others) + 1):
            args += [f"-disposition:a:{n}", "0"]
        title = f"AC3 {'5.1' if channels > 2 else 'stereo'}"
    if stream.get("language"):
        args += [f"-metadata:s:a:{new}", f"language={stream['language']}"]
    args += [f"-metadata:s:a:{new}", f"title={title}"]
    args += ["-f", "matroska", target]
    try:
        result = throttle.run(args, timeout=4 * 3600)
        if result.returncode != 0 or not os.path.exists(target):
            raise MediaToolError(
                "ffmpeg failed: "
                + (result.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
            )
        after = probe(target)
        count = lambda i, kind: sum(1 for s in i.streams if s.get("codec_type") == kind)
        added = [s for s in after.streams if s.get("codec_type") == "audio"][new]
        if (
            abs(after.duration - info.duration) > 2
            or len(after.chapters) != len(info.chapters)
            or count(after, "video") != count(info, "video")
            or count(after, "audio") != count(info, "audio") + 1
            or count(after, "subtitle") != count(info, "subtitle")
            or added.get("codec_name") != codec
        ):
            raise MediaToolError("The rewritten file didn't check out; left unchanged")
        os.replace(target, path)
    finally:
        if os.path.exists(target):
            os.unlink(target)
    return probe(path)


@_exclusive
def remove_chapters(path: str) -> None:
    info = probe(path)
    if info.mp4:
        rewrite_mp4(path, None)
        return
    _mkvpropedit(remove_command(path))


def export_chapters_xml(path: str) -> str | None:
    """The file's own chapters as Matroska XML (editions, UIDs and all); for
    an MP4, as an ffmetadata text (restore_chapters_xml knows either)."""
    info = probe(path)
    if info.mp4:
        return ffmetadata(info.chapters, info.duration) if info.chapters else None
    extract = tool("mkvextract")
    if not extract:
        return None
    with tempfile.TemporaryDirectory(prefix="curatarr-chapters-") as directory:
        target = Path(directory) / "chapters.xml"
        result = _run([extract, path, "chapters", str(target)], timeout=300)
        if result.returncode not in (0, 1) or not target.exists():
            return None
        return target.read_text(encoding="utf-8")


@_exclusive
def restore_chapters_xml(path: str, xml: str) -> None:
    if xml.startswith(FFMETADATA):
        rewrite_mp4(path, xml)
        return
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", prefix="curatarr-chapters-", encoding="utf-8", delete=False
    ) as handle:
        handle.write(xml)
        chapter_file = handle.name
    try:
        _mkvpropedit([required_tool("mkvpropedit"), path, "--chapters", chapter_file])
    finally:
        os.unlink(chapter_file)


def speech_stream(info: MediaInfo) -> int | None:
    """The audio stream to transcribe: the default track, else the first one."""
    audio = [s for s in info.streams if s["codec_type"] == "audio"]
    if not audio:
        return None
    return next((s for s in audio if s.get("default")), audio[0])["index"]


def extract_audio(
    path: str, target: str, stream: int, timeout: int = 3600, seconds=None, start=0
) -> None:
    """16 kHz mono WAV of one audio stream (or ``seconds`` of it from ``start``)."""
    limit = ["-t", str(seconds)] if seconds else []
    seek = ["-ss", str(start)] if start else []
    result = _run(
        [
            required_tool("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-y",
            *seek,
            "-i",
            path,
            *limit,
            "-map",
            f"0:{stream}",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            target,
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise MediaToolError(
            "Audio extraction failed: "
            + (result.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
        )
