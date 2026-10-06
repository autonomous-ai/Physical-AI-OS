"""Japanese speech pools must never silently fall back to English."""

import re
from string import Formatter

from hal.i18n import (
    DEFAULT_FILLERS_BY_LANG,
    HEAD_PAT_PHRASES_BY_LANG,
    MIC_MUTED_PHRASES_BY_LANG,
    MIC_UNMUTED_PHRASES_BY_LANG,
    MUSIC_BACKCHANNEL_POOLS,
    PHRASES_BY_LANG,
    localized_phrase,
)
from hal.presets import LANG_EN, LANG_JA, SUPPORTED_LANGS


def test_japanese_phrases_preserve_format_fields():
    assert LANG_JA in SUPPORTED_LANGS
    for key, translations in PHRASES_BY_LANG.items():
        japanese = translations[LANG_JA]
        assert localized_phrase(key, LANG_JA) == japanese
        assert re.search(r"[ぁ-んァ-ヶ一-龯]", japanese), key
        fields = lambda text: {name for _, name, _, _ in Formatter().parse(text) if name}
        assert fields(japanese) == fields(translations[LANG_EN]), key


def test_japanese_interaction_pools_are_complete():
    for pools in (
        HEAD_PAT_PHRASES_BY_LANG,
        MIC_MUTED_PHRASES_BY_LANG,
        MIC_UNMUTED_PHRASES_BY_LANG,
    ):
        assert len(pools[LANG_JA]) == len(pools[LANG_EN])
        assert all(re.search(r"[ぁ-んァ-ヶ一-龯]", line) for line in pools[LANG_JA])
    assert DEFAULT_FILLERS_BY_LANG[LANG_JA].split(",")


def test_japanese_music_tags_match_plain_pool():
    plain = MUSIC_BACKCHANNEL_POOLS[(LANG_JA, False)]
    tagged = MUSIC_BACKCHANNEL_POOLS[(LANG_JA, True)]
    assert len(plain) == len(MUSIC_BACKCHANNEL_POOLS[(LANG_EN, False)])
    assert [re.sub(r"^\[[a-z]+\] ", "", line) for line in tagged] == plain


def test_elevenlabs_japanese_catalog_and_websocket_mapping():
    from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
    from hal.drivers.voice.tts.elevenlabs_ws import ElevenLabsWSTTSBackend

    expected = ["Shizuka", "Konoha", "Rin", "Asahi", "Hinata", "Hiroki"]
    for language in ("ja", "ja-JP"):
        assert ElevenLabsTTSBackend.voices_for_language(language) == expected
    pool = ElevenLabsTTSBackend.VOICE_IDS_BY_LANG[LANG_JA]
    assert len(set(pool.values())) == 6
    for name in expected:
        assert name in ElevenLabsTTSBackend.voices_for_language("")
        assert ElevenLabsWSTTSBackend.VOICE_IDS[name] == pool[name]


def test_japanese_locale_aliases_reach_all_phrase_consumers(monkeypatch):
    import hal.config
    from hal.drivers import button_actions, os_shutdown
    from hal.drivers.voice import backchannel
    from hal.drivers.voice._internal.realtime_turn import _reply_language_name
    from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
    from hal.i18n import PHRASE_REBOOT
    from hal.realtime.config import _load_language
    from hal.routes import music

    for alias in ("ja", "ja-JP", "ja_JP", " JA-jp "):
        monkeypatch.setattr(hal.config, "_os_cfg_get", lambda *args: alias)
        assert localized_phrase(PHRASE_REBOOT, alias) == "再起動します。"
        assert button_actions._current_lang() == LANG_JA
        assert button_actions._factory_reset_phrase() == "工場出荷時の設定に戻します。再起動します。"
        assert os_shutdown._phrase(PHRASE_REBOOT) == "再起動します。"
        assert backchannel._default_fillers_for_active_lang() == DEFAULT_FILLERS_BY_LANG[LANG_JA]
        assert music._active_stt_language() == LANG_JA
        assert _load_language() == LANG_JA
        assert _reply_language_name() == "Japanese"
        assert ElevenLabsTTSBackend.voices_for_language(alias)[0] == "Shizuka"
