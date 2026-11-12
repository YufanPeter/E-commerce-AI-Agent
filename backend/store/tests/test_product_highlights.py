from unittest.mock import patch

from store import product_highlights as ph


def _point(emoji="🔊", title="Loud clear sound", detail="Two drivers stay clear at full volume."):
    return {"emoji": emoji, "title": title, "detail": detail}


def test_well_formed_points_are_kept():
    points = ph.clean_points([_point(), _point("💧", "IPX6 splash proof", "Survives shower spray, not full submersion.")])
    assert [p["emoji"] for p in points] == ["🔊", "💧"]


def test_checkmarks_repeats_and_long_text_are_dropped():
    points = ph.clean_points([
        _point(),
        _point("✅", "Checkmark", "Seller style."),
        _point("🔊", "Same emoji again", "Repeats the first icon."),
        _point("🔋", "Battery", " ".join(["word"] * 30)),
        _point("A", "Letter emoji", "Not an emoji."),
        _point("📏", "Compact size.", "Fits in a jacket pocket."),
    ])
    assert [(p["emoji"], p["title"]) for p in points] == [("🔊", "Loud clear sound"), ("📏", "Compact size")]


def test_too_few_points_mean_none():
    assert ph.clean_points([_point()]) == []
    assert ph.clean_points("not a list") == []


def test_points_are_cached_per_listing(tmp_path):
    store = ph.HighlightStore(tmp_path / "catalog.sqlite3")
    written = [_point(), _point("💧", "Splash proof", "Handles rain.")]
    with patch.object(ph, "write_points", return_value=written) as write:
        assert store.points_for("p1", "Speaker", "Listing text") == written
        assert store.points_for("p1", "Speaker", "Listing text") == written
        assert write.call_count == 1
        store.points_for("p1", "Speaker", "Edited listing text")
        assert write.call_count == 2


def test_failures_are_not_cached(tmp_path):
    store = ph.HighlightStore(tmp_path / "catalog.sqlite3")
    with patch.object(ph, "write_points", return_value=[]) as write:
        assert store.points_for("p1", "Speaker", "Listing") == []
        store.points_for("p1", "Speaker", "Listing")
        assert write.call_count == 2


def test_empty_listing_skips_the_model(tmp_path):
    with patch.object(ph, "write_points") as write:
        assert ph.HighlightStore(tmp_path / "c.sqlite3").points_for("p1", "Speaker", "  ") == []
        write.assert_not_called()
