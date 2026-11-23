"""English dubs with gaps: stretches of a dub left in the film's own language.

Dubs are often made from a cut that's missing a few scenes, and the release
fills them with the original audio: the film plays in English, then switches
to French for four minutes, then back. The dub check (dubs.py) samples only a
handful of points, so it can't see these. For a film made in another language
whose default track is a real English dub, the whole track is listened to in
WINDOW-second pieces, and each piece with speech in it is given a language.
Pieces in the film's own language, at least MIN_WINDOWS of them close
together, are a gap; each gap is translated into English and written beside
the film as forced subtitles (``Film.en.forced.srt``), which players show on
their own for an English track.

A subtitle file someone else put there is never replaced; Curatarr's own is
remembered in the dub check's result and rewritten when the file changes.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import gpu, throttle
from .mediafile import MediaToolError, extract_audio

WINDOW = 30  # seconds of audio given one language
MIN_SPEECH = 0.2  # a piece needs at least this share spoken to count
CONFIDENT = 0.6  # and Whisper this sure of its language
MIN_WINDOWS = 2  # pieces in the original language before it's a gap
JOIN = 60  # pieces this close (seconds between) are one gap
PAD = 5  # seconds translated either side of a gap
SUFFIX = ".en.forced.srt"
# Whisper's turbo model was trained without translation: asked to translate,
# it writes out the French. A model that translates does that part.
TRANSLATOR = "medium"


def gaps(windows, original: str) -> list:
    """[(start, end)] stretches in the ``original`` language, from
    [(start, language, probability)] for each piece with speech in it."""
    foreign = sorted(
        start
        for start, language, probability in windows
        if language == original and probability >= CONFIDENT
    )
    found, run = [], []
    for start in foreign:
        if run and start - (run[-1] + WINDOW) > JOIN:
            found.append(run)
            run = []
        run.append(start)
    if run:
        found.append(run)
    return [(run[0], run[-1] + WINDOW) for run in found if len(run) >= MIN_WINDOWS]


def _stamp(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def srt(segments) -> str:
    """SubRip text from [{"start", "end", "text"}]."""
    cues = []
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        cues.append(
            f"{len(cues) + 1}\n{_stamp(segment['start'])} --> "
            f"{_stamp(segment['end'])}\n{text}\n"
        )
    return "\n".join(cues)


def subtitle_path(film_path: str) -> Path:
    path = Path(film_path)
    return path.with_name(path.stem + SUFFIX)


def scan(info, track: int, original: str, device: str, model: str, make_room=None):
    """Listen to the whole of ``track``: ([(start, end)] gaps, [translated
    segments in them, timed from the film's start])."""
    with tempfile.TemporaryDirectory(prefix="curatarr-dub-gaps-") as directory:
        audio = str(Path(directory) / "track.wav")
        extract_audio(info.path, audio, track, timeout=1800)
        from .transcribe import _cuda_library_path

        env = os.environ | {"LD_LIBRARY_PATH": _cuda_library_path()}
        try:
            with gpu.exclusive():
                if make_room and device != "cpu":
                    make_room()
                result = throttle.run(
                    [
                        sys.executable,
                        "-m",
                        "curatarr.dub_gaps",
                        "--audio",
                        audio,
                        "--original",
                        original,
                        "--model",
                        model,
                        "--device",
                        device,
                    ],
                    env=env,
                    timeout=max(1800, int(info.duration * 2)),
                )
        except subprocess.TimeoutExpired as exc:
            raise MediaToolError("Listening for gaps in the dub timed out") from exc
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise MediaToolError(f"Listening for gaps failed: {message[:200]}")
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise MediaToolError("Listening for gaps produced no readable output") from exc
    return [tuple(g) for g in found["gaps"]], found["segments"]


def _listen(audio_path: str, original: str, model_name: str, device: str) -> dict:
    from faster_whisper import WhisperModel, decode_audio
    from faster_whisper.vad import get_speech_timestamps

    model = _model(WhisperModel, model_name, device)
    audio = decode_audio(audio_path)
    rate = 16000
    step = WINDOW * rate
    windows = []
    for offset in range(0, len(audio), step):
        piece = audio[offset : offset + step]
        try:
            spoken = sum(t["end"] - t["start"] for t in get_speech_timestamps(piece))
        except (RuntimeError, ValueError, IndexError):
            continue
        if spoken / max(len(piece), 1) < MIN_SPEECH:
            continue
        try:
            language, probability, _all = model.detect_language(
                audio=piece, vad_filter=True
            )
        except (RuntimeError, ValueError, IndexError):
            continue
        windows.append([offset // rate, language, round(float(probability), 3)])
    found = gaps(windows, original)
    if found and "turbo" in model_name:
        model = _model(WhisperModel, TRANSLATOR, device)
    return {
        "windows": windows,
        "gaps": found,
        "segments": _translate(model, audio, found, original),
    }


def _model(whisper_model, model_name, device):
    attempts = (
        [("cuda", "float16"), ("cpu", "int8")]
        if device == "auto"
        else [(device, "float16" if device == "cuda" else "int8")]
    )
    last_error = None
    for name, compute in attempts:
        try:
            return whisper_model(model_name, device=name, compute_type=compute)
        except (RuntimeError, ValueError, OSError) as exc:
            last_error = exc
    raise RuntimeError(f"faster-whisper could not run: {last_error}")


def _translate(model, audio, found, original) -> list:
    rate = 16000
    segments = []
    for start, end in found:
        begin = max(0, start - PAD)
        piece = audio[begin * rate : (end + PAD) * rate]
        translated, _info = model.transcribe(
            piece,
            task="translate",
            language=original,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        segments += [
            {
                "start": round(s.start + begin, 2),
                "end": round(s.end + begin, 2),
                "text": s.text,
            }
            for s in translated
        ]
    return segments


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--original", required=True)
    parser.add_argument("--model", default="medium")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    args = parser.parse_args(argv)
    print(json.dumps(_listen(args.audio, args.original, args.model, args.device)))


if __name__ == "__main__":
    main()
