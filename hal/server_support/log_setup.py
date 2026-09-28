"""HAL logging configuration: colored stdout + rotating file + optional GELF."""

import logging
import logging.handlers
import os
from pathlib import Path

_LEVEL_COLORS = {
    logging.DEBUG: "\033[37m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
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
    """Configure root logging (console + rotating file + GELF); call once, early. Returns the `hal.server` logger."""
    configured_log_dir = os.environ.get("HAL_LOG_DIR")
    log_dir = Path(configured_log_dir or "/var/log/hal")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        writable = os.access(log_dir, os.W_OK)
    except PermissionError:
        writable = False
    if not writable:
        # Explicit HAL_LOG_DIR and production devices fail loud; only the mock body falls back.
        if configured_log_dir:
            raise PermissionError(f"HAL_LOG_DIR is not writable: {log_dir}")
        if os.environ.get("HAL_MODE", "production").strip().lower() != "developer":
            raise PermissionError(f"production log directory is not writable: {log_dir}")
        log_dir = Path("/tmp/autonomous-hal")
        log_dir.mkdir(parents=True, exist_ok=True)

    _root = logging.getLogger()
    _log_level = os.environ.get("HAL_LOG_LEVEL", "INFO").upper()
    _root.setLevel(getattr(logging, _log_level, logging.INFO))
    for noisy_name in ("google.genai", "websockets", "httpx", "httpcore"):
        logging.getLogger(noisy_name).setLevel(logging.WARNING)

    _console = logging.StreamHandler()
    _console.setFormatter(_ColorFormatter())
    _root.addHandler(_console)

    # 1 MB per file, 3 backups (~4 MB max).
    _file = logging.handlers.RotatingFileHandler(
        log_dir / "server.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=3,
    )
    _file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _root.addHandler(_file)

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

    # Child of the logger above; propagate=False keeps OpenAI lines out of gemini_usage.log.
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

    # A simulated body must stay fully local (no GELF traffic).
    from hal import config

    if os.environ.get("DEVICE_TYPE") != "sim" and not config.SIMULATE:
        try:
            from hal.drivers.gelf_handler import GELFHandler
            from hal.config import _os_cfg_get

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
