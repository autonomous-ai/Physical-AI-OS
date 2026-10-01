"""Tests for hal.drivers.gelf_handler — where HAL ships its GELF records."""

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


def test_handler_without_target_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    handler = GELFHandler(os_cfg_get=_cfg({}), spool_dir=str(tmp_path), start_worker=False)
    sent = []
    monkeypatch.setattr(handler, "_send", sent.append)
    handler.emit(logging.LogRecord("hal", logging.ERROR, __file__, 1, "boom", None, None))
    assert sent == []


def test_handler_relay_posts_bearer_without_basic_auth(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    _use_fake_requests(monkeypatch)
    handler = GELFHandler(
        os_cfg_get=_cfg({"llm_base_url": API_BASE, "llm_api_key": "lobster"}),
        spool_dir=str(tmp_path), start_worker=False,
    )
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


# --- spool: records that cannot ship yet are kept and replayed ---------------

class _Resp:
    def __init__(self, status):
        self.status_code = status


class _ScriptedSession(_FakeSession):
    """Answers with the next status from `statuses` (then 202)."""
    statuses = []

    def post(self, url, json=None, timeout=None):
        status = _ScriptedSession.statuses.pop(0) if _ScriptedSession.statuses else 202
        if status == 202:
            self.posts.append((url, json))
        return _Resp(status)


def _use_scripted_requests(monkeypatch, statuses):
    _FakeSession.instances = []
    _ScriptedSession.statuses = list(statuses)
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(
        Session=_ScriptedSession, adapters=types.SimpleNamespace(HTTPAdapter=lambda **kwargs: kwargs),
    ))
    monkeypatch.setattr("hal.drivers.gelf_handler.GELF_REPLAY_INTERVAL", 0)


def _record(msg):
    return logging.LogRecord("hal", logging.ERROR, __file__, 1, msg, None, None)


def test_setup_logs_wait_in_the_spool_until_the_key_arrives(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    _use_scripted_requests(monkeypatch, [])
    config = {}
    handler = GELFHandler(os_cfg_get=_cfg(config), spool_dir=str(tmp_path), start_worker=False)

    handler.emit(_record("wifi join failed"))  # first setup: no key, no internet
    assert _FakeSession.instances == []

    config.update({"llm_base_url": API_BASE, "llm_api_key": "lobster", "device_id": "dev-1"})
    handler.refresh_target()
    assert handler.replay_once() is True

    posts = _FakeSession.instances[0].posts
    assert [p[1]["short_message"] for p in posts] == ["wifi join failed"]
    assert posts[0][1]["_spooled"] == "true"
    assert posts[0][1]["host"] == "dev-1"  # filed under the device, not the hostname
    assert posts[0][0] == API_BASE + "/logs/gelf"


def test_failed_sends_replay_in_order_after_the_collector_recovers(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    _use_scripted_requests(monkeypatch, [503, 503])
    handler = GELFHandler(
        os_cfg_get=_cfg({"llm_base_url": API_BASE, "llm_api_key": "lobster"}),
        spool_dir=str(tmp_path), start_worker=False,
    )
    assert handler._send({"short_message": "one", "host": handler._host}) is False
    assert handler._send({"short_message": "two", "host": handler._host}) is False

    assert handler.replay_once() is True
    posts = _FakeSession.instances[0].posts
    assert [p[1]["short_message"] for p in posts] == ["one", "two"]


def test_replay_stops_at_the_first_failure_and_keeps_the_rest(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    _use_scripted_requests(monkeypatch, [503, 503, 202, 503])
    handler = GELFHandler(
        os_cfg_get=_cfg({"llm_base_url": API_BASE, "llm_api_key": "lobster"}),
        spool_dir=str(tmp_path), start_worker=False,
    )
    handler._send({"short_message": "a", "host": handler._host})
    handler._send({"short_message": "b", "host": handler._host})
    assert handler.replay_once() is False  # "a" ships, "b" fails again
    assert handler.replay_once() is True
    # "a" shipped once in the first round, "b" in the second: nothing resent.
    assert [p[1]["short_message"] for p in _FakeSession.instances[-1].posts] == ["a", "b"]


def test_replay_never_ships_another_devices_records(monkeypatch, tmp_path):
    # Records a previous owner's device could not ship stay in the spool; after
    # a factory reset and a new setup they must not go out with the new key.
    monkeypatch.delenv("GELF_URL", raising=False)
    _use_scripted_requests(monkeypatch, [])
    config = {}
    handler = GELFHandler(os_cfg_get=_cfg(config), spool_dir=str(tmp_path), start_worker=False)
    handler._spool.append({"short_message": "previous owner's speech", "host": "previous-device"})
    handler.emit(_record("setup: wifi joined"))  # this boot, before the device id is known

    config.update({"llm_base_url": API_BASE, "llm_api_key": "new-owner", "device_id": "new-device"})
    handler.refresh_target()
    assert handler.replay_once() is True

    posts = _FakeSession.instances[0].posts
    assert [(p[1]["short_message"], p[1]["host"]) for p in posts] == [("setup: wifi joined", "new-device")]


def test_rejected_record_is_dropped_not_retried(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    _use_scripted_requests(monkeypatch, [413])
    handler = GELFHandler(
        os_cfg_get=_cfg({"llm_base_url": API_BASE, "llm_api_key": "lobster"}),
        spool_dir=str(tmp_path), start_worker=False,
    )
    assert handler._send({"short_message": "too big"}) is True
    assert handler._spool.pending() is False


def test_spool_is_bounded_and_drops_the_oldest(tmp_path):
    from hal.drivers.gelf_handler import GELFSpool

    spool = GELFSpool(str(tmp_path), "hal", max_bytes=1000)
    for i in range(200):
        spool.append({"short_message": f"line-{i:03d}"})
    lines = spool.take()
    assert b"line-199" in lines[-1]
    assert b"line-000" not in lines[0]
    assert sum(len(ln) + 1 for ln in lines) <= 1000


def test_direct_collector_mode_has_no_spool(monkeypatch, tmp_path):
    monkeypatch.setenv("GELF_URL", "https://logs.example/gelf")
    handler = GELFHandler(os_cfg_get=_cfg({}), spool_dir=str(tmp_path), start_worker=False)
    assert handler._spool is None


def test_backoff_grows_only_with_failed_replays_and_resets_on_success():
    from hal.drivers.gelf_handler import GELF_RETRY_MAX, GELF_RETRY_MIN, next_retry_delay

    assert next_retry_delay(0.0, True) == 0.0
    assert next_retry_delay(0.0, False) == GELF_RETRY_MIN
    assert next_retry_delay(GELF_RETRY_MIN, False) == GELF_RETRY_MIN * 2
    assert next_retry_delay(GELF_RETRY_MAX, False) == GELF_RETRY_MAX
    assert next_retry_delay(120.0, True) == 0.0


def test_backing_off_worker_ignores_wakeups_from_failed_live_sends(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    handler = GELFHandler(os_cfg_get=_cfg({}), spool_dir=str(tmp_path), start_worker=False)
    slept = []
    monkeypatch.setattr("hal.drivers.gelf_handler.time.sleep", slept.append)
    handler._wake.set()  # a failed live send while the network is down
    handler._sleep_until_next_attempt(40.0)
    assert slept == [40.0]  # the full backoff, not cut short by the wake event
    assert not handler._wake.is_set()
