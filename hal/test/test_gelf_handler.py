"""Tests for hal.drivers.gelf_handler — where HAL ships its GELF records.

GELF_URL set → straight to that collector with basic auth, as before. Unset →
relayed through the cloud API (`{base}/logs/gelf`) with the device's own key,
so no Graylog credential has to live on the device.
"""

import logging
import sys
import types

from hal.drivers.gelf_handler import GELFHandler, resolve_target

API_BASE = "https://device-api.autonomous.ai/api/v1/ai/v1"


def _cfg(values):
    """Stand-in for hal.config._os_cfg_get over an in-memory config.json."""
    return lambda key, default="": values.get(key, default)


class _FakeSession:
    instances = []

    def __init__(self):
        self.auth = None
        self.headers = {}
        self.posts = []
        _FakeSession.instances.append(self)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))

    def mount(self, prefix, adapter):
        pass


def _use_fake_requests(monkeypatch):
    _FakeSession.instances = []
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(
        Session=_FakeSession, adapters=types.SimpleNamespace(HTTPAdapter=lambda **kwargs: kwargs),
    ))


def test_gelf_url_env_ships_direct_with_basic_auth():
    env = {"GELF_URL": "https://logs.example/gelf", "GELF_USERNAME": "u", "GELF_PASSWORD": "p"}
    url, auth, headers = resolve_target(env, _cfg({"llm_base_url": API_BASE, "llm_api_key": "k"}))
    assert url == "https://logs.example/gelf"
    assert auth == ("u", "p")
    assert headers == {}


def test_unset_gelf_url_relays_through_cloud_api_with_bearer():
    url, auth, headers = resolve_target({}, _cfg({"llm_base_url": API_BASE, "llm_api_key": "lobster"}))
    assert url == API_BASE + "/logs/gelf"
    assert auth is None
    assert headers == {"Authorization": "Bearer lobster"}


def test_relay_prefers_shipped_autonomous_defaults_over_byo_llm_fields():
    cfg = _cfg({
        "autonomous_defaults": {"base_url": API_BASE, "api_key": "shipped"},
        "llm_base_url": "https://openrouter.ai/api/v1",
        "llm_api_key": "byo",
    })
    url, _, headers = resolve_target({}, cfg)
    assert url == API_BASE + "/logs/gelf"
    assert headers == {"Authorization": "Bearer shipped"}


def test_relay_partial_defaults_fall_back_to_llm_fields():
    cfg = _cfg({
        "autonomous_defaults": {"base_url": API_BASE, "api_key": ""},
        "llm_base_url": API_BASE,
        "llm_api_key": "live",
    })
    _, _, headers = resolve_target({}, cfg)
    assert headers == {"Authorization": "Bearer live"}


def test_relay_ignores_non_dict_defaults():
    cfg = _cfg({"autonomous_defaults": "", "llm_base_url": API_BASE, "llm_api_key": "live"})
    url, _, headers = resolve_target({}, cfg)
    assert url == API_BASE + "/logs/gelf"
    assert headers == {"Authorization": "Bearer live"}


def test_relay_never_targets_a_byo_provider():
    cfg = _cfg({"llm_base_url": "https://openrouter.ai/api/v1", "llm_api_key": "byo"})
    assert resolve_target({}, cfg) == ("", None, {})


def test_relay_normalizes_unversioned_api_base():
    cfg = _cfg({"llm_base_url": "https://device-api.autonomous.ai/api/v1/ai/", "llm_api_key": "k"})
    url, _, _ = resolve_target({}, cfg)
    assert url == API_BASE + "/logs/gelf"


def test_no_credentials_leaves_the_handler_without_a_target():
    assert resolve_target({}, _cfg({})) == ("", None, {})
    assert resolve_target({}, None) == ("", None, {})


def test_handler_without_target_sends_nothing(monkeypatch):
    monkeypatch.delenv("GELF_URL", raising=False)
    handler = GELFHandler(os_cfg_get=_cfg({}))
    sent = []
    monkeypatch.setattr(handler, "_send", sent.append)
    handler.emit(logging.LogRecord("hal", logging.ERROR, __file__, 1, "boom", None, None))
    assert sent == []


def test_handler_relay_posts_bearer_without_basic_auth(monkeypatch):
    monkeypatch.delenv("GELF_URL", raising=False)
    _use_fake_requests(monkeypatch)
    handler = GELFHandler(os_cfg_get=_cfg({"llm_base_url": API_BASE, "llm_api_key": "lobster"}))
    handler._send({"short_message": "hi"})

    session = _FakeSession.instances[0]
    assert session.posts == [(API_BASE + "/logs/gelf", {"short_message": "hi"})]
    assert session.headers["Authorization"] == "Bearer lobster"
    assert session.auth is None


def test_handler_direct_keeps_basic_auth(monkeypatch):
    monkeypatch.setenv("GELF_URL", "https://logs.example/gelf")
    monkeypatch.setenv("GELF_USERNAME", "u")
    monkeypatch.setenv("GELF_PASSWORD", "p")
    _use_fake_requests(monkeypatch)
    handler = GELFHandler(os_cfg_get=_cfg({"llm_base_url": API_BASE, "llm_api_key": "lobster"}))
    handler._send({"short_message": "hi"})

    session = _FakeSession.instances[0]
    assert session.posts[0][0] == "https://logs.example/gelf"
    assert session.auth == ("u", "p")
    assert "Authorization" not in session.headers


def test_session_pools_enough_connections_for_log_bursts():
    from hal.drivers import gelf_handler

    handler = gelf_handler.GELFHandler.__new__(gelf_handler.GELFHandler)
    handler._session = None
    handler._auth = ("", "")
    handler._headers = {}
    session = handler._get_session()
    for prefix in ("https://", "http://"):
        assert session.get_adapter(prefix + "campaign-api.autonomous.ai")._pool_maxsize == gelf_handler.GELF_POOL_MAXSIZE == 32
