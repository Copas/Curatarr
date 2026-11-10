def test_a_speech_filter_hiccup_falls_back_to_the_whole_track():
    from curatarr.transcribe import _spoken_language

    class Model:
        def detect_language(self, audio, vad_filter=False):
            if vad_filter:
                raise IndexError("boolean index did not match indexed array")
            return "en", 0.9, []

    assert _spoken_language(Model(), "audio") == "en"
