"""Settings of removed features, left in saved options, don't break them."""

from curatarr import chapters


def test_a_removed_features_setting_is_ignored():
    options = chapters.validate_global_options({"nudity_index": True})
    assert "nudity_index" not in options
