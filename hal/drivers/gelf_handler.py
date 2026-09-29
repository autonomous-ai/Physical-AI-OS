"""GELF HTTP log handler for centralized logging to Graylog."""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

GELF_POOL_MAXSIZE = 32

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


class GELFHandler(logging.Handler):
    """Sends log records to a GELF HTTP endpoint. Fire-and-forget."""

    def __init__(self, service_name: str = "hal", os_cfg_get: Optional[Callable[..., Any]] = None):
        super().__init__(level=logging.INFO)
        self._host = socket.gethostname() or "hal"
        self._pid = os.getpid()
        self._service_name = service_name
        self._session = None
        self._url, self._auth, self._headers = resolve_target(os.environ, os_cfg_get)

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
        if not self._url:
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
            threading.Thread(target=self._send, args=(msg,), daemon=True).start()
        except Exception:
            pass

    def _send(self, msg):
        try:
            self._get_session().post(self._url, json=msg, timeout=3)
        except Exception:
            pass

    def set_host(self, host: str):
        if host:
            self._host = host
