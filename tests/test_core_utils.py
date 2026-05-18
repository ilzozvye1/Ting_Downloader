from ting13.core.download import is_valid_audio_url, reorder_with_gaps_first
from ting13.core.models import Chapter
from ting13.core.utils import sanitize_filename


def _chapter(index: int) -> Chapter:
    return Chapter(index=index, title=f"第{index}集", play_url=f"https://example.test/{index}")


def test_sanitize_filename_replaces_windows_reserved_chars():
    assert sanitize_filename('a<b>c:d/e\\f|g?h*i') == "a_b_c_d_e_f_g_h_i"


def test_sanitize_filename_uses_fallback_for_empty_name():
    assert sanitize_filename(" . ") == "untitled"


def test_reorder_with_gaps_first_prioritizes_missing_middle_chapters():
    chapters = [_chapter(i) for i in [3, 5, 6, 7]]
    ordered = reorder_with_gaps_first(chapters, downloaded_indices={1, 2, 4, 8})

    assert [chapter.index for chapter in ordered] == [3, 5, 6, 7]


def test_reorder_with_gaps_first_keeps_continuation_after_gaps():
    chapters = [_chapter(i) for i in [3, 5, 9, 10]]
    ordered = reorder_with_gaps_first(chapters, downloaded_indices={1, 2, 4, 8})

    assert [chapter.index for chapter in ordered] == [3, 5, 9, 10]


def test_audio_url_validation_rejects_player_pages():
    assert not is_valid_audio_url("https://example.test/MTaudio.php")
    assert not is_valid_audio_url("https://example.test/player.js")


def test_audio_url_validation_accepts_media_links():
    assert is_valid_audio_url("https://cdn.example.test/audio/1.mp3")
    assert is_valid_audio_url("//cdn.example.test/audio/1.m4a")
