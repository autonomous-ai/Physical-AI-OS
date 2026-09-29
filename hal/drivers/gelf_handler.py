"""GELF HTTP log handler for centralized logging to Graylog."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

GELF_POOL_MAXSIZE = 32

# Spool for records that cannot ship yet — mirrors system/lib/logger/spool.go.
# A first setup runs with no device key and no internet, so HAL's records from
# that window would otherwise be dropped; they are replayed once the relay can
# deliver. Same directory as os-server/bootstrap (OS_GELF_SPOOL_DIR), one
# bounded spool per service.
GELF_SPOOL_DIR = os.environ.get("OS_GELF_SPOOL_DIR", "/var/lib/autonomous/gelf-spool")
GELF_SPOOL_MAX_BYTES = 1 << 20
# The worker re-reads config.json this often (the key arrives mid-setup and a
# re-setup replaces it) and replays the spool when a target exists.
GELF_REFRESH_INTERVAL = 30.0
# Replay pacing: the cloud relay answers 202 at once and silently drops past
# its in-flight ceiling, so a backlog must not be sent at full speed.
GELF_REPLAY_INTERVAL = 0.1
# Backoff after a failed replay, as in os-server (system/lib/logger).
GELF_RETRY_MIN = 5.0
GELF_RETRY_MAX = 300.0
# Collector answers worth retrying later rather than dropping.
_RETRY_STATUSES = {401, 403, 408, 429}

_LEVEL_MAP = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}

_RELAY_PATH = "/logs/gelf"

_AUTONOMOUS_DOMAINS = ("autonomous.ai", "autonomousdev.xyz")


def _normalize_base(base: str) -> str:
    """Add the API version an older config.json may lack (mirrors
    system/lib/urlnorm.NormalizeBaseURL). os-server normalizes config.json only when it
    saves it, so a file never re-saved can still lack the version segment.
    """
    base = base.strip().rstrip("/")
    if base.endswith("/ai") and _is_autonomous_host(base):
        base += "/v1"
    return base


def _is_autonomous_host(base: str) -> bool:
    try:
        host = (urlparse(base).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in _AUTONOMOUS_DOMAINS)


def _relay_credentials(os_cfg_get: Callable[..., Any]) -> tuple[str, str]:
    """Pick the cloud API base URL + device key for the relay, or ("", "").

    Same rule as os-server's Config.GELFRelayCredentials.
    """
    candidates = []
    defaults = os_cfg_get("autonomous_defaults")
    if isinstance(defaults, dict):
        candidates.append((defaults.get("base_url"), defaults.get("api_key")))
    candidates.append((os_cfg_get("llm_base_url"), os_cfg_get("llm_api_key")))
    for raw_base, raw_key in candidates:
        base = _normalize_base(str(raw_base or ""))
        key = str(raw_key or "").strip()
        if key and _is_autonomous_host(base):
            return base, key
    return "", ""


def resolve_target(
    env: Mapping[str, str], os_cfg_get: Optional[Callable[..., Any]]
) -> tuple[str, Optional[tuple[str, str]], dict[str, str]]:
    """Return (url, basic_auth, headers) for shipping GELF records."""
    url = env.get("GELF_URL", "")
    if url:
        return url, (env.get("GELF_USERNAME", ""), env.get("GELF_PASSWORD", "")), {}
    if os_cfg_get is None:
        return "", None, {}
    base, key = _relay_credentials(os_cfg_get)
    if not base:
        return "", None, {}
    return base + _RELAY_PATH, None, {"Authorization": f"Bearer {key}"}


class GELFSpool:
    """Bounded on-disk spool of GELF records (JSON lines), oldest dropped first.

    Mirrors system/lib/logger/spool.go.
    """

    def __init__(self, directory: str, name: str, max_bytes: int = GELF_SPOOL_MAX_BYTES):
        os.makedirs(directory, exist_ok=True)
        self.active = os.path.join(directory, name + ".jsonl")
        self.backup = os.path.join(directory, name + ".1.jsonl")
        self.replay = os.path.join(directory, name + ".replay.jsonl")
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        # Fail here, not on the first record: an unwritable directory disables
        # the spool instead of turning every log call into an error.
        with open(self.active, "a"):
            pass

    def append(self, msg: dict) -> None:
        line = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        half = self.max_bytes // 2
        if len(line) > half:
            return
        with self._lock:
            try:
                size = os.path.getsize(self.active)
            except OSError:
                size = 0
            if size + len(line) > half:
                try:
                    os.replace(self.active, self.backup)
                except OSError:
                    pass
            with open(self.active, "ab") as f:
                f.write(line)

    def pending(self) -> bool:
        with self._lock:
            return any(_has_data(p) for p in (self.replay, self.backup, self.active))

    def take(self) -> list:
        """Move everything into the replay file and return its records, oldest first."""
        with self._lock:
            data = b""
            for p in (self.replay, self.backup, self.active):
                try:
                    with open(p, "rb") as f:
                        data += f.read()
                except FileNotFoundError:
                    pass
            while len(data) > self.max_bytes and b"\n" in data:
                data = data.split(b"\n", 1)[1]
            tmp = self.replay + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.replay)
            for p in (self.backup,):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
            open(self.active, "wb").close()
            return [ln for ln in data.split(b"\n") if ln.strip()]

    def keep(self, rest: list) -> None:
        """Rewrite the replay file with what is still undelivered (or remove it)."""
        with self._lock:
            if not rest:
                try:
                    os.remove(self.replay)
                except FileNotFoundError:
                    pass
                return
            tmp = self.replay + ".tmp"
            with open(tmp, "wb") as f:
                f.write(b"\n".join(rest) + b"\n")
            os.replace(tmp, self.replay)


def _has_data(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def next_retry_delay(current: float, ok: bool) -> float:
    """Backoff after a replay attempt: 0 once it succeeds, else 5s doubling to 5 min."""
    if ok:
        return 0.0
    return min(current * 2, GELF_RETRY_MAX) if current else GELF_RETRY_MIN


class GELFHandler(logging.Handler):
    """Sends log records to a GELF HTTP endpoint.

    In relay mode, records that cannot ship yet are spooled and replayed once
    the relay delivers; GELF_URL (direct collector) stays fire-and-forget.
    """

    def __init__(
        self,
        service_name: str = "hal",
        os_cfg_get: Optional[Callable[..., Any]] = None,
        spool_dir: Optional[str] = None,
        start_worker: bool = True,
    ):
        super().__init__(level=logging.INFO)
        self._default_host = socket.gethostname() or "hal"
        self._host = self._default_host
        self._pid = os.getpid()
        self._service_name = service_name
        self._session = None
        self._os_cfg_get = os_cfg_get
        self._direct = bool(os.environ.get("GELF_URL", ""))
        self._url, self._auth, self._headers = resolve_target(os.environ, os_cfg_get)
        self._spool = None
        if not self._direct and os_cfg_get is not None:
            try:
                self._spool = GELFSpool(spool_dir or GELF_SPOOL_DIR, service_name)
            except OSError:
                self._spool = None
        self._wake = threading.Event()
        if self._spool is not None and start_worker:
            threading.Thread(target=self._worker, name="gelf-replay", daemon=True).start()

    def _get_session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
            # Every record posts on its own thread, so a log burst (TTS timing, realtime
            # turns) opens more than requests' default 10 pooled connections to one host
            # and urllib3 discards the extras with a "Connection pool is full" warning
            # (lamp-ee17, 2026-09-25).
            adapter = requests.adapters.HTTPAdapter(pool_maxsize=GELF_POOL_MAXSIZE)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
            self._session.auth = self._auth
            self._session.headers["Content-Type"] = "application/json"
            self._session.headers.update(self._headers)
        return self._session

    def emit(self, record):
        if not self._url and self._spool is None:
            return
        try:
            msg = {
                "version": "1.1",
                "host": self._host,
                "short_message": self.format(record),
                "timestamp": record.created,
                "level": _LEVEL_MAP.get(record.levelno, 6),
                "_service_name": self._service_name,
                "_level_name": record.levelname,
                "_logger": record.name,
                "_pid": self._pid,
            }
            if not self._url:
                self._spool.append(msg)  # no key yet: keep it for later
                return
            threading.Thread(target=self._send, args=(msg,), daemon=True).start()
        except Exception:
            pass

    def _send(self, msg) -> bool:
        """Post one record. A failure worth retrying lands in the spool."""
        ok = self._post(msg)
        if not ok and self._spool is not None:
            self._spool.append(msg)
            self._wake.set()
        return ok

    def _post(self, msg) -> bool:
        try:
            resp = self._get_session().post(self._url, json=msg, timeout=3)
        except Exception:
            return False
        status = getattr(resp, "status_code", 202)
        if 200 <= status < 300:
            return True
        # 4xx other than these will never be accepted: drop rather than retry.
        return not (status in _RETRY_STATUSES or status >= 500)

    def refresh_target(self) -> None:
        """Re-read the relay target and device id from config.json.

        A first setup saves the key while HAL is running and a re-setup replaces
        it; resolving only at startup shipped nothing until the next restart.
        """
        if self._direct or self._os_cfg_get is None:
            return
        target = resolve_target(os.environ, self._os_cfg_get)
        if target != (self._url, self._auth, self._headers):
            self._url, self._auth, self._headers = target
            self._session = None  # rebuild with the new credential
        device_id = self._os_cfg_get("device_id")
        if device_id:
            self.set_host(str(device_id))

    def replay_once(self) -> bool:
        """Ship the spool oldest-first, paced. True when it is empty afterwards."""
        if self._spool is None or not self._url or not self._spool.pending():
            return True
        lines = self._spool.take()
        for i, line in enumerate(lines):
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # corrupt line: skip rather than stall the replay
            msg["_spooled"] = "true"
            if msg.get("host") == self._default_host and self._host != self._default_host:
                msg["host"] = self._host
            if not self._post(msg):
                self._spool.keep(lines[i:])
                return False
            time.sleep(GELF_REPLAY_INTERVAL)
        self._spool.keep([])
        return not self._spool.pending()

    def _worker(self):
        retry = 0.0  # 0 while healthy; the current backoff after a failed replay
        while True:
            self._sleep_until_next_attempt(retry)
            try:
                self.refresh_target()
                ok = self.replay_once()
            except Exception:
                ok = False
            retry = next_retry_delay(retry, ok)

    def _sleep_until_next_attempt(self, retry: float) -> None:
        """Healthy: wait the refresh interval, or less if a failed send wakes us.
        Backing off: sleep the whole backoff. While the network is down every
        live send fails and sets the wake event; honouring it would retry at
        once, fail again and double the backoff each time — a 44s outage pushed
        it to 5 minutes on a real device, delaying the replay long after the
        network came back."""
        if retry:
            time.sleep(retry)
        else:
            self._wake.wait(GELF_REFRESH_INTERVAL)
        self._wake.clear()

    def set_host(self, host: str):
        if host:
            self._host = host
