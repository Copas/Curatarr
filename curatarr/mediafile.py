"""Read and write chapter metadata without re-encoding (ffprobe, ffmpeg, MKVToolNix).

Only Matroska files are written, in place with mkvpropedit, which edits the
container's header elements and never touches the audio/video data. Other
containers would need a full remux, so they are reported as unsupported.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import throttle
from .chapters import Chapter, chapter_file_text

# Jellyfin's bundled FFmpeg is the usual copy on a Jellyfin host without a
# system FFmpeg.
TOOL_FALLBACK_DIRS = ("/usr/lib/jellyfin-ffmpeg",)
MATROSKA_FORMATS = {"matroska", "webm", "matroska,webm"}
MATROSKA_SUFFIXES = {".mkv", ".mka", ".mk3d", ".webm"}


class MediaToolError(RuntimeError):
    pass


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
    def stream_signature(self) -> list[tuple]:
        """What must be identical before and after a chapter edit."""
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

    result = run(hardware=True)
    if result.returncode != 0:
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
    if not info.matroska:
        return False, (
            f"{info.format_name or 'This'} container cannot take chapters without "
            "rewriting the whole file; only Matroska (MKV) is edited in place."
        )
    if not tool("mkvpropedit"):
        return False, "mkvpropedit (MKVToolNix) is not installed"
    if not os.access(info.path, os.W_OK):
        return False, "Curatarr does not have permission to modify this file"
    return True, None


def _mkvpropedit(args):
    result = _run(args, timeout=600)
    # mkvpropedit exits 1 for warnings with a successful edit, 2 for errors.
    if result.returncode not in (0, 1):
        raise MediaToolError(
            "mkvpropedit failed: "
            + (result.stdout.strip().splitlines() or ["unknown error"])[-1][:200]
        )
    return result


def write_chapters(path: str, chapters: list[Chapter]) -> None:
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


def remove_tracks(
    path: str, info: MediaInfo, drop: list[int], default_audio: int
) -> MediaInfo:
    """Rewrite the file without some tracks (a copy, never a re-encode).

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
    args.append(path)
    try:
        result = throttle.run(args, timeout=4 * 3600)
        if result.returncode not in (0, 1) or not os.path.exists(target):
            raise MediaToolError(
                "mkvmerge failed: "
                + (result.stdout.strip().splitlines() or ["unknown error"])[-1][:200]
            )
        after = probe(target)
        count = lambda i, kind: sum(1 for s in i.streams if s.get("codec_type") == kind)
        if (
            abs(after.duration - info.duration) > 2
            or len(after.chapters) != len(info.chapters)
            or count(after, "video") != count(info, "video")
            or count(after, "audio") != len(keep_audio)
            or count(after, "subtitle") != len(keep_subtitles)
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


def add_compatible_audio(path: str, info: MediaInfo, source: int) -> MediaInfo:
    """Add an AC3 copy of one audio track, first and default (5.1 at most).

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
    channels = min(stream.get("channels") or 6, 6)
    others = [s["index"] for s in info.streams if s.get("codec_type") == "audio"]
    args = [ffmpeg, "-v", "error", "-y", "-i", path, "-map", "0:v?"]
    args += ["-map", f"0:{source}"]
    for index in others:
        args += ["-map", f"0:{index}"]
    args += ["-map", "0:s?", "-map", "0:t?", "-map_chapters", "0", "-c", "copy"]
    args += ["-c:a:0", "ac3", "-b:a:0", "640k" if channels > 2 else "224k"]
    args += ["-ac:a:0", str(channels), "-disposition:a:0", "default"]
    if stream.get("language"):
        args += ["-metadata:s:a:0", f"language={stream['language']}"]
    args += ["-metadata:s:a:0", f"title=AC3 {'5.1' if channels > 2 else 'stereo'}"]
    for n in range(1, len(others) + 1):
        args += [f"-disposition:a:{n}", "0"]
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
        first = next(s for s in after.streams if s.get("codec_type") == "audio")
        if (
            abs(after.duration - info.duration) > 2
            or len(after.chapters) != len(info.chapters)
            or count(after, "video") != count(info, "video")
            or count(after, "audio") != count(info, "audio") + 1
            or count(after, "subtitle") != count(info, "subtitle")
            or first.get("codec_name") != "ac3"
        ):
            raise MediaToolError("The rewritten file didn't check out; left unchanged")
        os.replace(target, path)
    finally:
        if os.path.exists(target):
            os.unlink(target)
    return probe(path)


def remove_chapters(path: str) -> None:
    _mkvpropedit(remove_command(path))


def export_chapters_xml(path: str) -> str | None:
    """The file's own chapters as Matroska XML (editions, UIDs and all)."""
    extract = tool("mkvextract")
    if not extract:
        return None
    with tempfile.TemporaryDirectory(prefix="curatarr-chapters-") as directory:
        target = Path(directory) / "chapters.xml"
        result = _run([extract, path, "chapters", str(target)], timeout=300)
        if result.returncode not in (0, 1) or not target.exists():
            return None
        return target.read_text(encoding="utf-8")


def restore_chapters_xml(path: str, xml: str) -> None:
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
