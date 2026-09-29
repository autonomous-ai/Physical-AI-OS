#!/usr/bin/env python3
"""Stdlib HTTP client for the device buddy daemon (:5002); network helpers never raise."""

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone

CONFIG_PATH = os.path.expanduser("~/.config/claude-code-buddy.json")
BUDDY_PORT = 5002


def load_config():
    """Load the Mac-side config, or return an empty dict on any error."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    """Write the config as 0600, creating the parent dir as needed."""
    d = os.path.dirname(CONFIG_PATH)
    os.makedirs(d, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def default_device(cfg):
    """Return the device matching `default_host`, else the first device, else None."""
    devices = cfg.get("devices", [])
    if not devices:
        return None
    host = cfg.get("default_host")
    if host:
        for dev in devices:
            if dev.get("host") == host or dev.get("last_known_ip") == host:
                return dev
    return devices[0]


def device_addr(dev):
    """Best address to reach a device: prefer last_known_ip, else host."""
    if not dev:
        return None
    return dev.get("last_known_ip") or dev.get("host")


def auth_headers(dev):
    """Bearer header with the device admin password, or {} if none is stored."""
    pw = (dev or {}).get("password")
    return {"Authorization": f"Bearer {pw}"} if pw else {}


def health(addr, timeout=0.5):
    """True if GET http://<addr>:5002/health reports status == 'ok'."""
    if not addr:
        return False
    try:
        req = urllib.request.Request(f"http://{addr}:{BUDDY_PORT}/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            info = json.loads(resp.read())
            return info.get("status") == "ok"
    except Exception:
        return False


def send(path, payload, dev=None):
    """POST JSON to the device daemon; True on HTTP 200, else False."""
    if dev is None:
        dev = default_device(load_config())
    addr = device_addr(dev)
    if not addr:
        return False
    try:
        req = urllib.request.Request(
            f"http://{addr}:{BUDDY_PORT}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **auth_headers(dev)},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def request_approval(payload, dev=None, timeout=55):
    """Long-poll for a permission decision: "allow" | "deny" | "timeout", or None on error."""
    if dev is None:
        dev = default_device(load_config())
    addr = device_addr(dev)
    if not addr:
        return None
    try:
        req = urllib.request.Request(
            f"http://{addr}:{BUDDY_PORT}/claude-code/approval-request",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **auth_headers(dev)},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read()).get("decision")
    except Exception:
        return None


def _find_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _find_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_strings(v)


def get_token():
    """Return the Claude Code OAuth token from ~/.claude/.credentials.json or the macOS Keychain."""
    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.exists(cred_path):
        try:
            with open(cred_path) as f:
                data = json.load(f)
            for v in _find_strings(data):
                if v.startswith("sk-ant-oat"):
                    return v
        except Exception:
            pass
    try:
        kc = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True,
        )
        if kc.returncode == 0:
            data = json.loads(kc.stdout.strip())
            for v in _find_strings(data):
                if v.startswith("sk-ant-oat"):
                    return v
    except Exception:
        pass
    return None


def fetch_usage(token):
    """Fetch OAuth usage from the Anthropic API."""
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def time_left(iso_str):
    """Humanize remaining time until an ISO timestamp ('1h 56m', '1d 11h')."""
    if not iso_str:
        return "N/A"
    try:
        diff = datetime.fromisoformat(iso_str) - datetime.now(timezone.utc)
    except Exception:
        return "N/A"
    total_sec = int(diff.total_seconds())
    if total_sec <= 0:
        return "now"
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    if h > 24:
        return f"{h // 24}d {h % 24}h"
    return f"{h}h {m}m"
