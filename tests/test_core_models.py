from ting13.core.models import BookInfo, Chapter
from ting13.core.download import DownloadCallbacks


def test_chapter_dataclass_fields():
    ch = Chapter(index=1, title="测试章节", play_url="https://example.test/1")
    assert ch.index == 1
    assert ch.title == "测试章节"
    assert ch.play_url == "https://example.test/1"
    assert ch.audio_url == ""
    assert ch.downloaded is False


def test_chapter_with_audio_url():
    ch = Chapter(index=2, title="测试", play_url="https://x/2", audio_url="https://cdn/2.mp3")
    assert ch.audio_url == "https://cdn/2.mp3"


def test_bookinfo_dataclass():
    ch1 = Chapter(index=1, title="第1集", play_url="https://x/1")
    ch2 = Chapter(index=2, title="第2集", play_url="https://x/2")
    book = BookInfo(title="测试书", author="作者", chapters=[ch1, ch2], source_name="test")
    assert book.title == "测试书"
    assert book.author == "作者"
    assert len(book.chapters) == 2
    assert book.source_name == "test"


def test_bookinfo_defaults():
    book = BookInfo(title="无作者", chapters=[], source_name="t")
    assert book.author == ""
    assert book.extra == {}


def test_download_callbacks_defaults():
    cb = DownloadCallbacks()
    assert cb.on_log is not None
    assert cb.is_stopped is not None
    assert not cb.is_stopped()


def test_download_callbacks_custom():
    logs = []
    cb = DownloadCallbacks(
        on_log=lambda msg: logs.append(msg),
        is_stopped=lambda: True,
    )
    cb.on_log("test message")
    assert logs == ["test message"]
    assert cb.is_stopped()
