"""HAL logging configuration — colored stdout + rotating file + optional GELF.

Extracted verbatim from server.py so the boot module stays focused on the
hardware/route wiring. `setup_logging()` carries the exact same side effects
(root-logger handlers) and must be called once, early, before any driver import
emits a warning. Returns the `hal.server` logger the boot module logs through.
"""

import logging
import logging.handlers
import os
from pathlib import Path

_LEVEL_COLORS = {
    logging.DEBUG: "\033[37m",  # gray
    logging.INFO: "\033[32m",  # green
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """Adds ANSI colors to levelname for console output."""

    _fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    def format(self, record):
        color = _LEVEL_COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname}{_RESET}"
        formatter = logging.Formatter(self._fmt)
        return formatter.format(record)


def setup_logging() -> logging.Logger:
    """Configure root logging (console + rotating file + GELF) and return the
    `hal.server` logger. Idempotent enough for a single boot call."""
    configured_log_dir = os.environ.get("HAL_LOG_DIR")
    log_dir = Path(configured_log_dir or "/var/log/hal")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        writable = os.access(log_dir, os.W_OK)
    except PermissionError:
        writable = False
    if not writable:
        # A laptop developer running the mock body has no reason to need sudo
        # merely to create the production log directory. Keep an explicit
        # HAL_LOG_DIR authoritative — a bad operator-supplied path must fail
        # loud — and make a production device fail loud too.
        if configured_log_dir:
            raise PermissionError(f"HAL_LOG_DIR is not writable: {log_dir}")
        if os.environ.get("HAL_MODE", "production").strip().lower() != "developer":
            raise PermissionError(f"production log directory is not writable: {log_dir}")
        log_dir = Path("/tmp/autonomous-hal")
        log_dir.mkdir(parents=True, exist_ok=True)

    _root = logging.getLogger()
    _log_level = os.environ.get("HAL_LOG_LEVEL", "INFO").upper()
    _root.setLevel(getattr(logging, _log_level, logging.INFO))
    # Keep HAL's own INFO breadcrumbs while preventing SDK/websocket internals
    # from dumping raw frames into server.log when their loggers are verbose.
    for noisy_name in ("google.genai", "websockets", "httpx", "httpcore"):
        logging.getLogger(noisy_name).setLevel(logging.WARNING)

    # Console handler (colored)
    _console = logging.StreamHandler()
    _console.setFormatter(_ColorFormatter())
    _root.addHandler(_console)

    # File handler: 1 MB per file, keep 3 backups (~4 MB max) -- no color codes
    _file = logging.handlers.RotatingFileHandler(
        log_dir / "server.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=3,
    )
    _file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _root.addHandler(_file)

    # Dedicated realtime token/cost log — its own file, kept OUT of server.log
    # and the console (propagate=False) so per-turn "[realtime] Gemini usage"
    # lines don't drown the main log and can be tailed/parsed on their own.
    _usage = logging.getLogger("hal.realtime.usage")
    _usage.setLevel(logging.DEBUG)
    _usage_file = logging.handlers.RotatingFileHandler(
        log_dir / "gemini_usage.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    _usage_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _usage.addHandler(_usage_file)
    _usage.propagate = False

    # OpenAI twin of the gemini usage log: "hal.realtime.usage.openai" is a
    # CHILD of the logger above, so propagate=False here keeps OpenAI lines out
    # of gemini_usage.log — one file per provider, comparable line-for-line.
    _usage_openai = logging.getLogger("hal.realtime.usage.openai")
    _usage_openai.setLevel(logging.DEBUG)
    _usage_openai_file = logging.handlers.RotatingFileHandler(
        log_dir / "openai_usage.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    _usage_openai_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _usage_openai.addHandler(_usage_openai_file)
    _usage_openai.propagate = False

    # GPT-Live twin: per-session-minute usage ("hal.realtime.usage.gptlive"),
    # kept in its own file for the same reason.
    _usage_gptlive = logging.getLogger("hal.realtime.usage.gptlive")
    _usage_gptlive.setLevel(logging.DEBUG)
    _usage_gptlive_file = logging.handlers.RotatingFileHandler(
        log_dir / "gptlive_usage.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    _usage_gptlive_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _usage_gptlive.addHandler(_usage_gptlive_file)
    _usage_gptlive.propagate = False

    # Pipecat v1 twin: per-turn TTFB + LLM token lines ("hal.realtime.usage.pipecat").
    _usage_pipecat = logging.getLogger("hal.realtime.usage.pipecat")
    _usage_pipecat.setLevel(logging.DEBUG)
    _usage_pipecat_file = logging.handlers.RotatingFileHandler(
        log_dir / "pipecat_usage.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    _usage_pipecat_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _usage_pipecat.addHandler(_usage_pipecat_file)
    _usage_pipecat.propagate = False

    # GELF handler: send INFO+ logs to centralized Graylog. A simulated body
    # must be fully local — no surprise network traffic while a developer is
    # proving a skill on a laptop.
    from hal import config

    if os.environ.get("DEVICE_TYPE") != "sim" and not config.SIMULATE:
        try:
            from hal.drivers.gelf_handler import GELFHandler
            from hal.config import _os_cfg_get

            # config.json supplies the cloud API relay target when GELF_URL
            # is unset (the shipped case — no Graylog credential on the device).
            _gelf = GELFHandler(os_cfg_get=_os_cfg_get)
            _gelf.setFormatter(logging.Formatter("%(message)s"))
            _device_id = _os_cfg_get("device_id")
            if _device_id:
                _gelf.set_host(_device_id)
            _root.addHandler(_gelf)
        except Exception:
            pass

    logger = logging.getLogger("hal.server")
    logger.info("Logging to %s/server.log", log_dir)
    return logger
