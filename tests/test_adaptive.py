from curatarr import adaptive
from curatarr.adaptive import Controller, Reading, _describe


def decide(controller, reading, *, running, most=4, now):
    return controller.decide(reading, most=most, running=running, waiting=True, now=now)


def test_adds_jobs_one_at_a_time_while_there_is_room():
    c = Controller(limit=1)
    quiet = Reading(cpu=0.20, gpu=0.30, viewers=0, transcodes=0)
    assert decide(c, quiet, running=1, now=100).limit == 2
    assert (
        decide(c, quiet, running=2, now=130).limit == 2
    )  # waits a minute between steps
    assert decide(c, quiet, running=2, now=170).limit == 3
    assert decide(c, quiet, running=3, now=240).limit == 4
    assert decide(c, quiet, running=4, now=320).limit == 4  # never past the ceiling


def test_holds_when_the_machine_is_full_and_never_starts_more():
    c = Controller(limit=3)
    busy = Reading(cpu=0.95, gpu=0.70, viewers=0, transcodes=0)
    d = decide(c, busy, running=2, now=100)
    assert d.limit == 2 and not d.suspend  # running jobs finish; nothing new starts


def test_viewers_and_transcodes_lower_the_target():
    c = Controller(limit=2)
    one = decide(c, Reading(0.3, 0.3, viewers=1, transcodes=0), running=2, now=0)
    two = decide(
        Controller(limit=2),
        Reading(0.3, 0.3, viewers=2, transcodes=1),
        running=2,
        now=0,
    )
    assert one.target > two.target >= adaptive.FLOOR
    # A load that's fine with nobody watching is too much with a transcode going.
    load = Reading(0.62, 0.40, viewers=1, transcodes=1)  # CPU 62% vs a 55% target
    assert decide(Controller(limit=2), load, running=2, now=0).limit == 2
    assert (
        decide(Controller(limit=2), Reading(0.62, 0.40, 0, 0), running=2, now=100).limit
        == 3
    )


def test_pauses_only_for_a_lasting_overload_and_does_not_flap():
    c = Controller(limit=3)
    swamped = Reading(cpu=0.97, gpu=0.95, viewers=1, transcodes=1)
    assert not decide(c, swamped, running=3, now=100).suspend  # a spike isn't enough
    d = decide(c, swamped, running=3, now=131)
    assert d.suspend and d.limit == 0
    # Paused, Curatarr's own load is gone; it stays paused for the minimum time.
    calm = Reading(cpu=0.20, gpu=0.10, viewers=1, transcodes=1)
    assert decide(c, calm, running=3, now=160).suspend
    resumed = decide(c, calm, running=3, now=260)
    assert not resumed.suspend and resumed.limit == 1
    # Same load with nobody watching: hold, don't suspend.
    idle = Controller(limit=3)
    assert not decide(idle, Reading(0.97, 0.95, 0, 0), running=3, now=100).suspend
    assert not decide(idle, Reading(0.97, 0.95, 0, 0), running=3, now=200).suspend


def test_gpu_only_counts_while_jellyfin_transcodes():
    """Direct play doesn't use the GPU; Curatarr's own transcription does."""
    assert Reading(cpu=0.20, gpu=0.95, viewers=1, transcodes=0).load == 0.20
    assert Reading(cpu=0.20, gpu=0.95, viewers=1, transcodes=1).load == 0.95


def test_counts_viewers_and_transcodes_from_sessions():
    sessions = [
        {"NowPlayingItem": {"Name": "x"}, "PlayState": {"PlayMethod": "DirectPlay"}},
        {"NowPlayingItem": {"Name": "y"}, "PlayState": {"PlayMethod": "Transcode"}},
        {"DeviceName": "idle phone"},
    ]
    assert adaptive.viewers(sessions) == (2, 1)


def test_reason_says_what_it_saw():
    d = Controller(limit=1).decide(
        Reading(0.40, 0.50, 1, 0), most=4, running=1, waiting=True, now=0
    )
    assert "CPU 40%" in d.reason and "GPU 50%" in d.reason and "1 watching" in d.reason


def test_jellyfins_video_decoder_is_not_counted():
    """Two transcodes kept the decoder at 97% while compute was at 15%: that
    paused all work for as long as anyone watched, though Curatarr's jobs
    don't use the decoder then (scene detection decodes on the CPU)."""
    reading = Reading(cpu=0.14, gpu=0.15, viewers=3, transcodes=2, decoder=0.97)
    assert reading.load == 0.15
    assert "video decoder 97% (Jellyfin's)" in _describe(reading, 0.15, 0.35)
    assert not decide(Controller(), reading, running=1, now=0).suspend


def test_scene_detection_leaves_the_decoder_to_jellyfin(monkeypatch):
    from types import SimpleNamespace

    from curatarr import mediafile

    calls = []
    monkeypatch.setattr(mediafile, "required_tool", lambda name: name)
    monkeypatch.setattr(
        mediafile,
        "_run",
        lambda args, timeout=None: (
            calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(adaptive, "leave_decoder", True)
    mediafile.detect_scenes("/x.mkv", 0.3)
    monkeypatch.setattr(adaptive, "leave_decoder", False)
    mediafile.detect_scenes("/x.mkv", 0.3)
    assert "-hwaccel" not in calls[0] and "-hwaccel" in calls[1]
