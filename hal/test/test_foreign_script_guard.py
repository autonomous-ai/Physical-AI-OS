"""A realtime reply in a script the device can't speak is dropped."""
from hal.drivers.voice._internal.realtime_turn import reply_is_foreign_script


def test_cjk_kana_hangul_dropped_on_english():
    for s in ["こんにちは", "안녕하세요", "你好世界", "元気ですか"]:
        assert reply_is_foreign_script(s, "English"), s


def test_vietnamese_dropped_on_english():
    for s in ["Đúng vậy, hôm nay trời thật dễ chịu.", "Chào bạn nhé", "Được rồi"]:
        assert reply_is_foreign_script(s, "English"), s


def test_english_kept():
    for s in ["I'm doing well, thanks!", "Sure, turning that off.", "Hello there."]:
        assert not reply_is_foreign_script(s, "English"), s


def test_matching_language_kept():
    # A Vietnamese device may speak Vietnamese; a Japanese device Japanese.
    assert not reply_is_foreign_script("Đúng vậy nhé", "Vietnamese")
    assert not reply_is_foreign_script("こんにちは", "Japanese")


if __name__ == "__main__":
    test_cjk_kana_hangul_dropped_on_english()
    test_vietnamese_dropped_on_english()
    test_english_kept()
    test_matching_language_kept()
    print("ok")
