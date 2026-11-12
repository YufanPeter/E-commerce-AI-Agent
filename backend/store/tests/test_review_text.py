import pytest

from store.review_text import clean_review_text


@pytest.mark.parametrize("raw, expected", [
    ("Mi primer bocina [[VIDEOID:cc296bbd27f64f2d57d84ceefe6965fa]] Muy contento",
     "Mi primer bocina Muy contento"),
    ("[[VIDEOID:abc123]]Great sound.", "Great sound."),
    ("Better than [[ASIN:B003UHXY62 Pantene Pro-V Restore Beautiful Lengths Shampoo]] for me",
     "Better than Pantene Pro-V Restore Beautiful Lengths Shampoo for me"),
    ('She said &#34;that smell yucky mommy!&#34;', 'She said "that smell yucky mommy!"'),
    ("NO WAY&lt;NEVER AGAIN", "NO WAY<NEVER AGAIN"),
    ("Line one<br />Line two", "Line one\nLine two"),
    ("Plain review.", "Plain review."),
    (None, ""),
])
def test_review_markup_is_removed(raw, expected):
    assert clean_review_text(raw) == expected
