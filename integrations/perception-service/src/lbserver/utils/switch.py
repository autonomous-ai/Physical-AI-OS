"""Two-slot deploy switch: which local dlserver slot lbserver sends traffic to.

The deploy script writes the active slot's URL to the state file and sends SIGHUP;
lbserver re-reads it and writes an ack next to it. Only the loopback entry of
LB__BACKENDS is replaced, so remote slave nodes stay in rotation.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "") in _LOOPBACK


def read_active(state_file: Path) -> str | None:
    """Active local backend URL from the state file; None if the file is missing or blank."""
    try:
        text = state_file.read_text().strip()
    except FileNotFoundError:
        return None
    if not text:
        return None
    url = text.rstrip("/")
    if not is_loopback(url):
        raise ValueError(f"{state_file}: {url!r} is not a loopback URL")
    return url


def resolve_backends(configured: list[str], active_local: str | None) -> list[str]:
    """Replace loopback entries of `configured` with `active_local`, keeping remote ones
    in order.
    """
    if active_local is None or not any(is_loopback(b) for b in configured):
        return list(configured)
    out: list[str] = []
    for backend in configured:
        chosen = active_local if is_loopback(backend) else backend
        if chosen not in out:
            out.append(chosen)
    return out


def write_ack(state_file: Path, backends: list[str]) -> None:
    """Record what this process applied; the deploy script trusts only this file."""
    ack = state_file.with_name(state_file.name + ".applied")
    tmp = ack.with_name(ack.name + ".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "backends": backends}))
    tmp.replace(ack)
