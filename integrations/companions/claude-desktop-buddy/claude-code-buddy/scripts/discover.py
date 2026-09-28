#!/usr/bin/env python3
"""Discover device buddy daemons on the LAN (cache, then mDNS, then /health sweep)."""

import concurrent.futures
import json
import re
import subprocess
import sys

from buddy_client import health, load_config

SERVICE_TYPE = "_autonomous._tcp"


def cache_check():
    """Re-verify each configured device by its last_known_ip /health."""
    cfg = load_config()
    found = []
    for dev in cfg.get("devices", []):
        ip = dev.get("last_known_ip")
        if ip and health(ip):
            found.append({"host": dev.get("host"), "ip": ip})
    return found


def _run_dns_sd(args, timeout=2):
    try:
        proc = subprocess.Popen(
            ["dns-sd"] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
        return stdout
    except FileNotFoundError:
        return ""


def mdns_discover():
    """Browse _autonomous._tcp, resolve hostnames + IPv4, verify on :5002."""
    stdout = _run_dns_sd(["-B", SERVICE_TYPE], timeout=2)

    services = []
    for line in stdout.splitlines():
        if "Add" in line and SERVICE_TYPE in line:
            parts = line.split()
            if len(parts) >= 7:
                services.append(parts[6])

    found = []
    for svc in services:
        out = _run_dns_sd(["-L", svc, SERVICE_TYPE], timeout=2)
        hostname = None
        for line in out.splitlines():
            if "can be reached at" in line:
                m = re.search(r"can be reached at\s+(\S+)", line)
                if m:
                    hostname = m.group(1).rstrip(".")
                    break
        if not hostname:
            continue

        out = _run_dns_sd(["-G", "v4", hostname], timeout=2)
        for line in out.splitlines():
            if "Add" in line:
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    ip = m.group(1)
                    if health(ip, timeout=1):
                        found.append({"host": hostname, "ip": ip})
                    break
    return found


def get_subnets():
    try:
        out = subprocess.check_output(["ifconfig"], text=True)
    except Exception:
        return []
    subnets = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet ") and not line.startswith("inet 127."):
            m = re.search(r"inet\s+(\d+\.\d+\.\d+)\.\d+", line)
            if m and m.group(1) not in subnets:
                subnets.append(m.group(1))
    return subnets


def http_sweep(subnets):
    """Probe every /24 host on :5002/health with a small thread pool."""
    candidates = []
    for subnet in subnets:
        candidates.extend(f"{subnet}.{i}" for i in range(1, 255))

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(health, ip, 0.5): ip for ip in candidates}
        for fut in concurrent.futures.as_completed(futures):
            ip = futures[fut]
            try:
                if fut.result():
                    found.append({"host": None, "ip": ip})
            except Exception:
                pass
    return found


def scan():
    """Discover via mDNS, falling back to a /health sweep; deduplicated by ip."""
    found = mdns_discover()
    if not found:
        subnets = get_subnets()
        if subnets:
            found = http_sweep(subnets)

    seen = set()
    merged = []
    for dev in found:
        ip = dev["ip"]
        if ip not in seen:
            seen.add(ip)
            merged.append(dev)
    return merged


def main():
    found = cache_check()
    if not found:
        found = scan()

    if found:
        seen = set()
        unique = []
        for dev in found:
            if dev["ip"] not in seen:
                seen.add(dev["ip"])
                unique.append(dev)
        print(json.dumps(unique))
    else:
        print(json.dumps({"error": "not_found"}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
