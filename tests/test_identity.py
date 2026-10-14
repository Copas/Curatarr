from curatarr import identity

SEASON = {
    20: "Brake My Wife, Please. When Homer loses his driver's license, Marge has to "
    "drive everyone everywhere, which becomes so stressful that she runs him over.",
    21: "The Bart of War. Bart and Milhouse are forced into community service after "
    "destroying Flanders' Beatles memorial and join separate scout groups.",
    22: "Moe Baby Blues. Moe saves Maggie from a car crash and becomes her babysitter, "
    "which leads to trouble with the mob.",
    19: "Old Yeller-Belly. Homer is called a coward after failing to save Grampa from "
    "a fire, while the dog Santa's Little Helper becomes a hero.",
}
DRIVING = (
    "Public transportation is for jerks. I miss my car so much, everyone's driving "
    "but me. Marge, you have to drive me to work. My license! Marge, the license. "
    "Driving driving driving. Homer walks. Marge drives the car everywhere."
)


def test_a_misfiled_episode_fits_another_summary_by_a_wide_margin():
    other, margin = identity.better_match(21, DRIVING, SEASON)
    assert other == 20 and margin >= identity.WIN_MARGIN


def test_a_correctly_filed_episode_is_not_flagged():
    assert identity.better_match(20, DRIVING, SEASON) is None


def test_vague_or_missing_summaries_flag_nothing():
    vague = {n: "The guys pull pranks." for n in range(1, 8)}
    assert identity.better_match(3, DRIVING, vague) is None
    assert identity.better_match(21, DRIVING, {20: SEASON[20], 21: SEASON[21]}) is None


def test_foreign_default_audio():
    streams = [
        {"codec_type": "audio", "language": "jpn", "default": True},
        {"codec_type": "audio", "language": "eng"},
    ]
    assert identity.foreign_audio("ja", streams) == ("ja", True)
    assert identity.foreign_audio("en", streams) is None
    assert identity.foreign_audio("ja", streams[:1]) == ("ja", False)
