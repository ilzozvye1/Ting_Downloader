from ting13.sources import find_source, get_source_names
from ting13.sources.huanting import HuantingSource
from ting13.sources.ting13 import Ting13Source


def test_registered_source_names_include_supported_sites():
    names = get_source_names()

    assert "ting13.cc" in names
    assert "ting22.com" in names
    assert "huanting.cc" in names


def test_find_source_matches_ting13_urls():
    source = find_source("https://www.ting13.cc/youshengxiaoshuo/10408/")

    assert isinstance(source, Ting13Source)
    assert source.detect_url_type("https://www.ting13.cc/youshengxiaoshuo/10408/") == "book"
    assert source.detect_url_type("https://www.ting13.cc/play/10408_1_253385.html") == "play"


def test_find_source_matches_huanting_urls():
    source = find_source("https://www.huanting.cc/book/2274.html")

    assert isinstance(source, HuantingSource)
    assert source.detect_url_type("https://www.huanting.cc/book/2274.html") == "book"
    assert source.detect_url_type("https://www.huanting.cc/ting/2274/1.html") == "play"


def test_find_source_returns_none_for_unknown_site():
    assert find_source("https://example.test/book/1") is None
