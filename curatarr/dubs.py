"""Checking that films made in another language really have English audio.

Radarr can grab an English dub of a foreign-language film, but uploads are
sometimes labelled English when the audio is the original. For films whose
original language isn't English, each audio track is sampled at a few points
mid-film (mostly dialogue) and Whisper says which language it hears. That
decides the fix: make a real English track the default, correct a track's
wrong language label, or flag the file as having no English audio so it can
be replaced.

Only films made in another language are checked: running this on everything
mostly measures music, which Whisper often mistakes for another language.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from . import gpu, throttle
from .mediafile import MediaInfo, MediaToolError, extract_audio

CLIPS = 5  # samples per track, spread over the middle of the film
CLIP_SECONDS = 20
POSITIONS = (0.2, 0.35, 0.5, 0.65, 0.8)
CONFIDENT = 0.6  # a sample counts only when Whisper is at least this sure
MIN_CONFIDENT = 3  # and a track's language needs this many such samples
AGREEMENT = 0.6  # of which this share must agree
MAX_TRACKS = 4  # commentary and extra dubs past this aren't worth the time

# Matroska language codes (ISO 639-2/B) and Radarr's names, by ISO 639-1.
LANGUAGES = {
    "en": ("eng", "English"),
    "fr": ("fre", "French"),
    "de": ("ger", "German"),
    "es": ("spa", "Spanish"),
    "it": ("ita", "Italian"),
    "pt": ("por", "Portuguese"),
    "nl": ("dut", "Dutch"),
    "da": ("dan", "Danish"),
    "sv": ("swe", "Swedish"),
    "no": ("nor", "Norwegian"),
    "fi": ("fin", "Finnish"),
    "is": ("ice", "Icelandic"),
    "pl": ("pol", "Polish"),
    "cs": ("cze", "Czech"),
    "hu": ("hun", "Hungarian"),
    "ro": ("rum", "Romanian"),
    "el": ("gre", "Greek"),
    "ru": ("rus", "Russian"),
    "uk": ("ukr", "Ukrainian"),
    "tr": ("tur", "Turkish"),
    "ar": ("ara", "Arabic"),
    "he": ("heb", "Hebrew"),
    "fa": ("per", "Persian"),
    "hi": ("hin", "Hindi"),
    "ta": ("tam", "Tamil"),
    "te": ("tel", "Telugu"),
    "th": ("tha", "Thai"),
    "vi": ("vie", "Vietnamese"),
    "id": ("ind", "Indonesian"),
    "ja": ("jpn", "Japanese"),
    "ko": ("kor", "Korean"),
    "zh": ("chi", "Chinese"),
}
# Other spellings found in files' language tags (639-2/T codes, names).
_ALIASES = {
    "fra": "fr",
    "deu": "de",
    "nld": "nl",
    "ces": "cs",
    "ron": "ro",
    "ell": "el",
    "fas": "fa",
    "isl": "is",
    "zho": "zh",
    "cmn": "zh",
    "yue": "zh",
    "nob": "no",
    "nno": "no",
}


def code(value) -> str | None:
    """A language tag, code or name as ISO 639-1, or None if unknown/undetermined."""
    text = str(value or "").strip().lower()
    if not text or text in {"und", "unk", "mul", "zxx", "mis"}:
        return None
    text = text.split("-")[0]  # IETF tags such as en-US
    if text in LANGUAGES:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    for short, (three, name) in LANGUAGES.items():
        if text in (three, name.lower()):
            return short
    return None


def name(short: str | None) -> str:
    return LANGUAGES[short][1] if short in LANGUAGES else (short or "unknown")


def heard(samples: list) -> str | None:
    """The language a track is in, from (language, probability) samples.

    None when the samples are too few or too unsure to say (silence, music,
    or a mix).
    """
    confident = [lang for lang, probability in samples if probability >= CONFIDENT]
    if len(confident) < MIN_CONFIDENT:
        return None
    language, count = Counter(confident).most_common(1)[0]
    return language if count / len(confident) >= AGREEMENT else None


def plan(streams: list[dict], heard_by_track: dict) -> dict:
    """What to do about a file's audio, given what each track sounded like.

    ``verdict`` is "english" (a real English track exists), "no_english"
    (every track was heard clearly and none is English) or "unsure".
    ``actions`` are ("label", track, language) for a wrong or missing label
    and ("default", track) to make a real English track the default.
    """
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    tracks, actions = [], []
    for stream in audio:
        index = stream["index"]
        label, sounds = code(stream.get("language")), heard_by_track.get(index)
        tracks.append(
            {
                "index": index,
                "label": label,
                "heard": sounds,
                "default": bool(stream.get("default")),
            }
        )
        # Relabel only where English is involved (the case that matters) or
        # the label is missing; one foreign label for another isn't worth
        # trusting a short sample over.
        if (
            sounds
            and sounds in LANGUAGES
            and sounds != label
            and (label is None or "en" in (label, sounds))
        ):
            actions.append(("label", index, sounds))
    english = [t["index"] for t in tracks if t["heard"] == "en"]
    if english:
        verdict = "english"
        default = next((t for t in tracks if t["default"]), None)
        # Switch only away from a default clearly in another language: one
        # that was just unclear may be the main English mix, and the other
        # English track a commentary.
        if default is None or (default["heard"] and default["heard"] != "en"):
            actions.append(("default", english[0]))
    elif tracks and all(t["heard"] for t in tracks):
        verdict = "no_english"
    else:
        verdict = "unsure"
    return {"verdict": verdict, "tracks": tracks, "actions": actions}


def removable(result: dict, streams: list[dict]) -> dict:
    """Tracks in other languages that can go, with a real English track kept.

    Audio goes only when it was heard clearly in another language; a track
    that couldn't be made out stays. Subtitles go by their label (they can't
    be listened to); English and unlabelled ones stay.
    """
    if result["verdict"] != "english":
        return {"audio": [], "subtitles": []}
    audio = [t["index"] for t in result["tracks"] if t["heard"] and t["heard"] != "en"]
    subtitles = [
        s["index"]
        for s in streams
        if s.get("codec_type") == "subtitle"
        and code(s.get("language")) not in (None, "en")
    ]
    return {"audio": audio, "subtitles": subtitles}


def sample_times(duration: float) -> list[float]:
    return [round(duration * p, 1) for p in POSITIONS[:CLIPS]]


def listen(info: MediaInfo, tracks: list[int], device: str, model: str, make_room=None):
    """{track: [(language, probability), ...]} for each audio track."""
    times = sample_times(info.duration)
    with tempfile.TemporaryDirectory(prefix="curatarr-dubs-") as directory:
        clips = {}
        for track in tracks:
            clips[track] = []
            for number, start in enumerate(times):
                target = str(Path(directory) / f"{track}-{number}.wav")
                extract_audio(
                    info.path,
                    target,
                    track,
                    timeout=300,
                    seconds=CLIP_SECONDS,
                    start=start,
                )
                clips[track].append(target)
        listing = Path(directory) / "clips.json"
        listing.write_text(json.dumps({str(k): v for k, v in clips.items()}))
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
                        "curatarr.dubs",
                        "--clips",
                        str(listing),
                        "--model",
                        model,
                        "--device",
                        device,
                    ],
                    env=env,
                    timeout=900,
                )
        except subprocess.TimeoutExpired as exc:
            raise MediaToolError("Listening to the audio tracks timed out") from exc
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise MediaToolError(f"Language detection failed: {message[:200]}")
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise MediaToolError("Language detection produced no readable output") from exc
    return {int(k): [tuple(s) for s in v] for k, v in found.items()}


def _detect(clips: dict, model_name: str, device: str) -> dict:
    from faster_whisper import WhisperModel, decode_audio

    attempts = (
        [("cuda", "float16"), ("cpu", "int8")]
        if device == "auto"
        else [(device, "float16" if device == "cuda" else "int8")]
    )
    last_error = None
    for name_, compute in attempts:
        try:
            model = WhisperModel(model_name, device=name_, compute_type=compute)
            break
        except (RuntimeError, ValueError, OSError) as exc:
            last_error = exc
    else:
        raise RuntimeError(f"faster-whisper could not run: {last_error}")
    found = {}
    for track, paths in clips.items():
        found[track] = []
        for path in paths:
            try:
                # The voice filter drops music and silence, which is what
                # Whisper most often mistakes for another language.
                language, probability, _all = model.detect_language(
                    audio=decode_audio(path), vad_filter=True
                )
            except (RuntimeError, ValueError, IndexError):
                continue  # nothing spoken in this clip
            found[track].append([language, round(float(probability), 3)])
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clips", required=True)
    parser.add_argument("--model", default="medium")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    args = parser.parse_args(argv)
    clips = json.loads(Path(args.clips).read_text())
    print(json.dumps(_detect(clips, args.model, args.device)))


if __name__ == "__main__":
    main()
