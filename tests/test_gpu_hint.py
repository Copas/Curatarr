"""Telling people whose graphics card goes unused to install chapters-gpu."""

from curatarr import transcribe


def _machine(monkeypatch, *, card, whisper=True, cuda=False):
    monkeypatch.setattr(transcribe, "nvidia_gpu", lambda: card)
    monkeypatch.setattr(transcribe, "available", lambda: whisper)
    monkeypatch.setattr(transcribe, "cuda_libraries", lambda: cuda)


def test_an_unused_card_is_pointed_out_on_enhance(app, client, monkeypatch):
    _machine(monkeypatch, card="NVIDIA GeForce RTX 3080 Ti")
    page = client.get("/chapters").text
    assert "Your NVIDIA GeForce RTX 3080 Ti isn't used for transcription" in page
    assert "EXTRAS=chapters-gpu" in page


def test_nothing_to_say_when_the_card_is_used_or_absent(app, client, monkeypatch):
    _machine(monkeypatch, card="NVIDIA GeForce RTX 3080 Ti", cuda=True)
    assert "isn't used for transcription" not in client.get("/chapters").text
    _machine(monkeypatch, card=None)
    assert "isn't used for transcription" not in client.get("/chapters").text


def test_settings_name_the_card_and_the_right_extra(app, client, monkeypatch):
    _machine(monkeypatch, card="NVIDIA GeForce RTX 3080 Ti")
    page = client.get("/chapters/settings").text
    assert "but this install can't use it" in page
    _machine(monkeypatch, card=None, whisper=False)
    page = client.get("/chapters/settings").text
    assert "no NVIDIA card found" in page
    assert "Reinstall with <code>EXTRAS=chapters</code>" in page


def test_recommended_extras_follow_the_card(monkeypatch):
    monkeypatch.setattr(transcribe, "nvidia_gpu", lambda: "Some card")
    assert transcribe.recommended_extras() == "chapters-gpu"
    monkeypatch.setattr(transcribe, "nvidia_gpu", lambda: None)
    assert transcribe.recommended_extras() == "chapters"


def test_cuda_libraries_tolerate_a_missing_nvidia_package(monkeypatch):
    def missing(_name):
        raise ModuleNotFoundError("nvidia")

    monkeypatch.setattr(transcribe.importlib.util, "find_spec", missing)
    assert transcribe.cuda_libraries() is False
