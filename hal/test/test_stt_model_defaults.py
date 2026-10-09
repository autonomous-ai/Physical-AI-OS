"""Default STT requests use Nova English and preserve explicit language/model choices."""
from urllib.parse import parse_qs, urlsplit

from hal.drivers.voice.stt.autonomous import AutonomousSTT


def params(**kwargs):
    provider = AutonomousSTT(api_key="test-key", base_url="https://example.invalid/v1", **kwargs)
    return parse_qs(urlsplit(provider._ws_url).query)


def test_default_request_uses_nova_english_not_none_language():
    query = params(keywords=["lamp:2"])
    assert query["model"] == ["nova-3-general"]
    assert query["language"] == ["en"]
    assert query["keyterm"] == ["lamp"]
    assert query["interim_results"] == ["true"]


def test_selected_language_is_preserved():
    assert params(language="vi")["language"] == ["vi"]


def test_explicit_flux_configuration_keeps_flux_protocol():
    query = params(model="flux-general-en")
    assert query["model"] == ["flux-general-en"]
    assert "endpointing" not in query
    assert "language" not in query
