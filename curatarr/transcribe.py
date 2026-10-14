"""Local speech-to-text with faster-whisper, run in a child process.

The child process keeps the model (and its GPU memory) out of the worker, and
lets a crash or timeout fail one job instead of the worker. Transcripts are
cached by the file's path, duration and audio tracks and the model, so an
unchanged file is never transcribed twice.

faster-whisper is an optional dependency: ``pip install curatarr[chapters]``.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import gpu, throttle
from .chapters import Segment
from .mediafile import MediaInfo, MediaToolError, extract_audio, speech_stream


class TranscriptionUnavailable(RuntimeError):
    pass


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


OPENING_SECONDS = 300  # enough to find a "previously on" recap


def cache_key(info: MediaInfo, model: str, opening=False, window=None) -> str:
    """Identifies the audio, not the file bytes.

    Adding chapters (or subtitles beside the file) changes the file's size and
    modification time but not its audio, so those must not invalidate the
    transcript. A replacement file (an upgrade) differs in duration or tracks.
    """
    audio = [
        (s.get("index"), s.get("codec_name"), s.get("language"))
        for s in info.streams
        if s.get("codec_type") == "audio"
    ]
    identity = f"{info.path}|{round(info.duration, 1)}|{audio}|{model}"
    if opening:
        identity += f"|opening{OPENING_SECONDS}"
    if window:
        identity += f"|window{int(window[0])}-{int(window[1])}"
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def cached(info: MediaInfo, model: str, cache_dir: Path, opening=False, window=None):
    """(segments, details) from the cache, or None.

    With ``opening`` or a ``window`` (start, end seconds) a full transcript
    serves too, trimmed to that part.
    """
    if opening:
        window = (0, OPENING_SECONDS)
        own = load(cache_dir / f"{cache_key(info, model, opening=True)}.json")
    else:
        own = None
    if window:
        full = cached(info, model, cache_dir)
        if full is not None:
            segments, details = full
            return [s for s in segments if window[0] <= s.start < window[1]], details
        if own is not None:
            return own
        return load(cache_dir / f"{cache_key(info, model, window=window)}.json")
    return load(cache_dir / f"{cache_key(info, model)}.json")


def load(path: Path):
    """(segments, details) from one transcript file, or None."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        segments = [Segment(s["start"], s["end"], s["text"]) for s in data["segments"]]
        return segments, data.get("details") or {}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cuda_library_path() -> str:
    """Directories of pip-installed CUDA runtime libraries (cuBLAS, cuDNN)."""
    directories = []
    for package in ("nvidia.cublas", "nvidia.cudnn"):
        spec = importlib.util.find_spec(package)
        if spec and spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                lib = Path(location) / "lib"
                if lib.is_dir():
                    directories.append(str(lib))
    existing = os.environ.get("LD_LIBRARY_PATH")
    return os.pathsep.join(directories + ([existing] if existing else []))


def transcribe(
    info: MediaInfo,
    model: str,
    device: str,
    cache_dir: Path,
    make_room=None,
    opening=False,
    window=None,
):
    """Segments for a media file, from the cache or a fresh transcription.

    Returns (segments, details) where details records the device and whether
    the cache was used. One transcription runs at a time, never alongside a
    language-model request (see gpu.py); ``make_room`` is called first to
    free GPU memory, typically by unloading Ollama's model. With ``opening``
    only the first five minutes are transcribed (for finding a recap); with a
    ``window`` (start, end seconds) only that part, timed from the file's start.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    hit = cached(info, model, cache_dir, opening, window)
    if hit is not None:
        segments, details = hit
        return segments, {**details, "cached": True}
    if not available():
        raise TranscriptionUnavailable(
            "faster-whisper is not installed (pip install 'curatarr[chapters]')"
        )
    stream = speech_stream(info)
    if stream is None:
        raise TranscriptionUnavailable("The file has no audio track")
    target = cache_dir / f"{cache_key(info, model, opening, window)}.json"
    start = window[0] if window else 0
    seconds = (
        (window[1] - window[0]) if window else (OPENING_SECONDS if opening else None)
    )
    with tempfile.TemporaryDirectory(prefix="curatarr-audio-") as directory:
        audio = str(Path(directory) / "audio.wav")
        extract_audio(info.path, audio, stream, seconds=seconds, start=start)
        env = os.environ | {"LD_LIBRARY_PATH": _cuda_library_path()}
        partial = target.with_suffix(".partial")
        try:
            with gpu.exclusive():
                if make_room and device != "cpu":
                    make_room()
                result = _run_transcriber(
                    audio, partial, model, device, env, info, offset=start
                )
        except subprocess.TimeoutExpired as exc:
            raise MediaToolError("Transcription timed out") from exc
        if result.returncode != 0 or not partial.exists():
            message = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
            raise MediaToolError(f"Transcription failed: {message[:200]}")
        partial.replace(target)
    hit = cached(info, model, cache_dir, opening, window)
    if hit is None:
        raise MediaToolError("Transcription produced no readable output")
    segments, details = hit
    return segments, {**details, "cached": False}


def _run_transcriber(audio, partial, model, device, env, info, offset=0):
    return throttle.run(
        [
            sys.executable,
            "-m",
            "curatarr.transcribe",
            "--audio",
            audio,
            "--out",
            str(partial),
            "--model",
            model,
            "--device",
            device,
            "--offset",
            str(offset),
        ],
        env=env,
        timeout=max(1800, int(info.duration * 3)),
    )


def _run_model(audio, model_name, device):
    from faster_whisper import WhisperModel

    attempts = (
        [("cuda", "float16"), ("cpu", "int8")]
        if device == "auto"
        else [(device, "float16" if device == "cuda" else "int8")]
    )
    last_error = None
    for name, compute in attempts:
        try:
            model = WhisperModel(model_name, device=name, compute_type=compute)
            segments, info = model.transcribe(
                audio, vad_filter=True, beam_size=5, condition_on_previous_text=False
            )
            rows = [
                {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text}
                for s in segments
            ]
            return rows, {
                "device": name,
                "compute_type": compute,
                "language": info.language,
                "model": model_name,
            }
        except (RuntimeError, ValueError, OSError) as exc:
            # A missing CUDA library or an unusable GPU surfaces here.
            last_error = exc
    raise RuntimeError(f"faster-whisper could not run: {last_error}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="medium")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument(
        "--offset",
        type=float,
        default=0.0,
        help="seconds the audio starts into the file",
    )
    args = parser.parse_args(argv)
    rows, details = _run_model(args.audio, args.model, args.device)
    for row in rows:
        row["start"] = round(row["start"] + args.offset, 2)
        row["end"] = round(row["end"] + args.offset, 2)
    Path(args.out).write_text(
        json.dumps({"segments": rows, "details": details}), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
