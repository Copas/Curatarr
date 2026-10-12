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
                "language": (s.get("tags") or {}).get("language"),
            }
            for s in data.get("streams") or []
        ],
    )


SCENE_LINE = re.compile(r"pts_time:([0-9.]+)")
SCORE_LINE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def detect_scenes(path: str, threshold: float, timeout: int = 4 * 3600):
    """Scene changes as (seconds, score), sampled at 5 frames per second.

    Frames are scaled down before comparison, which keeps a full-length film to
    a few minutes; hardware decoding is used when FFmpeg can.
    """
    result = _run(
        [
            required_tool("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-hwaccel",
            "auto",
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


def extract_audio(path: str, target: str, stream: int, timeout: int = 3600) -> None:
    """16 kHz mono WAV of one audio stream, for transcription."""
    result = _run(
        [
            required_tool("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-y",
            "-i",
            path,
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
