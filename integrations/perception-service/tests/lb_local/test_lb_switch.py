"""Two-slot deploy switch: lb.state_file + SIGHUP (lbserver/utils/switch.py, app.install_switch)."""

import json
import os
import signal

import httpx
import pytest
from fastapi.testclient import TestClient

from lbserver.utils.switch import read_active, resolve_backends, write_ack

LOCAL_A = "http://127.0.0.1:8001"
LOCAL_B = "http://127.0.0.1:8002"
REMOTE = "https://slave1:8899"


def _ack(state):
    return json.loads(state.with_name(state.name + ".applied").read_text())


class TestResolveBackends:
    def test_replaces_loopback_keeps_remote_order(self):
        assert resolve_backends([LOCAL_A, REMOTE], LOCAL_B) == [LOCAL_B, REMOTE]

    def test_no_active_keeps_configured(self):
        assert resolve_backends([LOCAL_A, REMOTE], None) == [LOCAL_A, REMOTE]

    def test_remote_only_config_ignores_active(self):
        assert resolve_backends([REMOTE], LOCAL_B) == [REMOTE]

    def test_localhost_spelling_is_loopback_and_deduped(self):
        assert resolve_backends(["http://localhost:8001", LOCAL_A], LOCAL_B) == [LOCAL_B]


class TestReadActive:
    def test_missing_file_is_none(self, tmp_path):
        assert read_active(tmp_path / "nope") is None

    def test_blank_file_is_none(self, tmp_path):
        f = tmp_path / "s"
        f.write_text("\n")
        assert read_active(f) is None

    def test_strips_newline_and_trailing_slash(self, tmp_path):
        f = tmp_path / "s"
        f.write_text(LOCAL_B + "/\n")
        assert read_active(f) == LOCAL_B

    def test_rejects_non_loopback(self, tmp_path):
        f = tmp_path / "s"
        f.write_text(REMOTE)
        with pytest.raises(ValueError):
            read_active(f)


def test_write_ack_format_is_the_deploy_script_contract(tmp_path):
    state = tmp_path / "dlserver-active"
    write_ack(state, [LOCAL_B])
    raw = (tmp_path / "dlserver-active.applied").read_text()
    assert f'"pid": {os.getpid()},' in raw  # deploy-dlserver.sh greps exactly this
    assert json.loads(raw) == {"pid": os.getpid(), "backends": [LOCAL_B]}


@pytest.fixture()
def switch(monkeypatch, tmp_path):
    """lbserver.app configured with [LOCAL_A, REMOTE] and a state file under tmp_path."""
    import lbserver.app as app_mod
    from lbserver.utils import RoundRobin

    state = tmp_path / "dlserver-active"
    monkeypatch.setattr(app_mod, "BACKENDS", [LOCAL_A, REMOTE])
    monkeypatch.setattr(app_mod, "STATE_FILE", state)
    monkeypatch.setattr(app_mod, "http_rr", RoundRobin([LOCAL_A, REMOTE]))
    monkeypatch.setattr(app_mod, "ws_rr", RoundRobin([LOCAL_A, REMOTE]))
    previous = signal.getsignal(signal.SIGHUP)
    yield app_mod, state
    signal.signal(signal.SIGHUP, previous)


def _picks(rr, n=4):
    return [rr.next() for _ in range(n)]


class TestInstallSwitch:
    def test_startup_without_state_file_uses_configured_and_acks(self, switch):
        app_mod, state = switch
        app_mod.install_switch()
        assert _picks(app_mod.http_rr) == [LOCAL_A, REMOTE, LOCAL_A, REMOTE]
        assert _ack(state)["backends"] == [LOCAL_A, REMOTE]

    def test_startup_follows_state_file(self, switch):
        app_mod, state = switch
        state.write_text(LOCAL_B + "\n")
        app_mod.install_switch()
        assert _picks(app_mod.http_rr) == [LOCAL_B, REMOTE, LOCAL_B, REMOTE]
        assert _picks(app_mod.ws_rr, 2) == [LOCAL_B, REMOTE]

    def test_sighup_switches_and_acks(self, switch):
        app_mod, state = switch
        app_mod.install_switch()
        state.write_text(LOCAL_B + "\n")
        os.kill(os.getpid(), signal.SIGHUP)
        assert _picks(app_mod.http_rr, 2) == [LOCAL_B, REMOTE]
        assert _ack(state) == {"pid": os.getpid(), "backends": [LOCAL_B, REMOTE]}

    def test_bad_state_file_keeps_current_backends(self, switch):
        app_mod, state = switch
        state.write_text(LOCAL_B)
        app_mod.install_switch()
        state.write_text(REMOTE)  # not loopback: rejected
        os.kill(os.getpid(), signal.SIGHUP)
        assert _picks(app_mod.http_rr, 2) == [LOCAL_B, REMOTE]
        assert _ack(state)["backends"] == [LOCAL_B, REMOTE]  # no new ack

    def test_off_when_no_state_file_configured(self, switch, monkeypatch):
        app_mod, _ = switch
        monkeypatch.setattr(app_mod, "STATE_FILE", None)
        before = signal.getsignal(signal.SIGHUP)
        app_mod.install_switch()
        assert signal.getsignal(signal.SIGHUP) is before


def test_proxy_follows_switch(switch, monkeypatch):
    app_mod, state = switch
    monkeypatch.setattr(app_mod, "BACKENDS", [LOCAL_A])
    monkeypatch.setattr(app_mod, "INTERNAL_PREFIX", "")
    monkeypatch.setattr(app_mod.settings.crypto, "enabled", False)
    seen: list[int] = []
    real_client = httpx.AsyncClient

    def _backend(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.port)
        return httpx.Response(200, json={})

    def _client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(_backend)
        return real_client(**kwargs)

    monkeypatch.setattr("lbserver.app.httpx.AsyncClient", _client)
    app_mod.install_switch()
    with TestClient(app_mod.app) as client:  # `with` runs the lifespan (pooled client)
        client.get("/hal/api/dl/health")
        state.write_text(LOCAL_B + "\n")
        os.kill(os.getpid(), signal.SIGHUP)
        client.get("/hal/api/dl/health")
    assert seen == [8001, 8002]
