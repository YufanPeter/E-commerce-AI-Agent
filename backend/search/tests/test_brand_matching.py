"""One brand spelled several ways by different sellers."""

import pytest

from search.catalog_search import same_brand


@pytest.mark.parametrize("stored, named", [
    ("Maybelline New York", "MAYBELLINE"),
    ("MAYBELLINE", "Maybelline New York"),
    ("L'Oreal Paris", "L'Oreal"),
    ("Wet n wild", "wet n wild"),
    ("SAMSUNG", "Samsung"),
])
def test_spellings_of_one_brand_match(stored, named):
    assert same_brand(stored, named)


@pytest.mark.parametrize("stored, named", [
    ("Applebee's", "Apple"),
    ("New Balance", "New Era"),
    ("", "Sony"),
])
def test_different_brands_do_not(stored, named):
    assert not same_brand(stored, named)
