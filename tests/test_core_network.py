from ting13.core.network import set_proxy, get_proxy, is_dns_poisoned
from ting13.sources.base import Source
from ting13.sources import find_source, get_source_names
from ting13.sources.ting13 import Ting13Source
from ting13.sources.huanting import HuantingSource


def test_set_and_get_proxy():
    set_proxy(None)
    assert get_proxy() is None

    set_proxy("http://127.0.0.1:7890")
    assert get_proxy() == "http://127.0.0.1:7890"

    set_proxy(None)
    assert get_proxy() is None


def test_source_base_log_func_defaults_to_print():
    source = find_source("https://www.ting13.cc/youshengxiaoshuo/10408/")
    assert source is not None
    assert source._log_func is print


def test_source_before_download_sets_log_func():
    source = find_source("https://www.huanting.cc/book/2274.html")
    assert source is not None
    logs = []
    from ting13.core.download import DownloadCallbacks
    cb = DownloadCallbacks(on_log=lambda msg: logs.append(msg))
    source.before_download([], cb)
    assert source._log_func is not print
    source._log_func("hello")
    assert logs == ["hello"]
    source.after_download()


def test_source_after_download_resets_log_func():
    source = find_source("https://www.huanting.cc/book/2274.html")
    assert source is not None
    from ting13.core.download import DownloadCallbacks
    cb = DownloadCallbacks()
    source.before_download([], cb)
    assert source._log_func is not print
    source.after_download()
    assert source._log_func is print


def test_find_source_ting13_variants():
    assert isinstance(find_source("https://ting13.cc/youshengxiaoshuo/1/"), Ting13Source)
    assert isinstance(find_source("https://m.ting13.cc/youshengxiaoshuo/1/"), Ting13Source)


def test_find_source_huanting_variants():
    assert isinstance(find_source("https://ting22.com/book/1.html"), HuantingSource)
    assert isinstance(find_source("https://www.huanting.cc/book/1.html"), HuantingSource)


def test_source_name_property():
    s = find_source("https://www.ting13.cc/youshengxiaoshuo/1/")
    assert s is not None
    assert "ting13" in s.name.lower() or "ting13.cc" in s.name.lower()
