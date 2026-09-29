#!/usr/bin/env python3
"""Report p50/p95 per turn stage from the robot's flow log.

    make latency TARGET=lamp-ac82.local PASSWORD=ac82 [DATE=2026-08-15]
    python3 scripts/bench/latency.py --file flow_2026-08-16.jsonl   # offline
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict

# Report order; unlisted nodes are still printed after these.
STAGE_ORDER = [
    "sensing_input",         # a mic/camera/sensor event opened a turn
    "chat_input",            # a text message opened a turn
    "voice_pipeline_start",  # the realtime voice path took it
    "intent_match",          # the local table answered it, no model
    "agent_call",            # the brain answered it
    "tool_call",
    "hw_emotion",            # markers fired at HAL
    "hw_servo",
    "hw_led",
    "hw_audio",
    "tts_send",              # the words went out
    "chat_send",
]


def login(host: str, password: str, timeout: float) -> str:
    """POST /api/login with the device password, return the bearer token."""
    req = urllib.request.Request(
        f"http://{host}/api/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    token = (body.get("data") or {}).get("token")
    if not token:
        raise SystemExit("login succeeded but returned no token")
    return token


def fetch_flow(host: str, token: str, date: str, timeout: float) -> list:
    """GET /api/agent/flow-logs — the day's JSONL, one flow event per line."""
    url = f"http://{host}/api/agent/flow-logs"
    if date:
        url += f"?date={date}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SystemExit(f"no flow log on {host} for {date or 'today'} — talk to the robot first")
        raise
    return parse_lines(raw.splitlines())


def parse_lines(lines) -> list:
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def stage_durations(events: list) -> dict:
    """node -> [duration_ms], from exit duration_ms or exit.ts - enter.ts."""
    durations = defaultdict(list)
    opened = {}
    for e in events:
        node = e.get("node") or "?"
        kind = e.get("kind")
        trace = e.get("trace_id") or ""
        key = (trace, node)
        if kind == "enter":
            opened[key] = e.get("ts")
        elif kind == "exit":
            ms = e.get("duration_ms")
            if not ms:
                started = opened.pop(key, None)
                if started and e.get("ts"):
                    ms = (e["ts"] - started) * 1000.0
            if ms:
                durations[node].append(float(ms))
    return durations


def turn_durations(events: list) -> list:
    """End-to-end per trace: last ts - first ts, in ms."""
    spans = defaultdict(lambda: [None, None])
    for e in events:
        trace, ts = e.get("trace_id"), e.get("ts")
        if not trace or not ts:
            continue
        span = spans[trace]
        span[0] = ts if span[0] is None else min(span[0], ts)
        span[1] = ts if span[1] is None else max(span[1], ts)
    return [(hi - lo) * 1000.0 for lo, hi in spans.values() if lo is not None and hi > lo]


def pct(values, p):
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = min(len(ordered) - 1, max(0, round((p / 100) * (len(ordered) - 1))))
    return ordered[idx]


def fmt(ms: float) -> str:
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


def report(events: list, host: str, date: str) -> int:
    if not events:
        raise SystemExit("no flow events found — talk to the robot, then run again")

    durations = stage_durations(events)
    turns = turn_durations(events)
    ordered = [n for n in STAGE_ORDER if n in durations]
    ordered += sorted(n for n in durations if n not in STAGE_ORDER)

    print(f"# Turn latency — {host or 'file'}, {date or 'today'}")
    print()
    print(f"{len(events)} flow events, {len(turns)} turns.")
    print()
    print("| Stage | n | p50 | p95 | max |")
    print("|---|---:|---:|---:|---:|")
    for node in ordered:
        vals = durations[node]
        print(f"| `{node}` | {len(vals)} | {fmt(pct(vals, 50))} | {fmt(pct(vals, 95))} | {fmt(max(vals))} |")
    if turns:
        print(f"| **whole turn** | {len(turns)} | **{fmt(pct(turns, 50))}** | **{fmt(pct(turns, 95))}** | {fmt(max(turns))} |")
    print()

    intent = durations.get("intent_match") or []
    agent = durations.get("agent_call") or []
    if intent:
        print(f"Fixed commands (`intent_match`, no model): p50 {fmt(pct(intent, 50))}, p95 {fmt(pct(intent, 95))}, n={len(intent)}.")
    if agent:
        print(f"Through the brain (`agent_call`): p50 {fmt(pct(agent, 50))}, p95 {fmt(pct(agent, 95))}, n={len(agent)}.")
    if not intent and not agent:
        print("Neither `intent_match` nor `agent_call` appeared — check the node names in system/ against STAGE_ORDER.")
    print()
    print("Measured from the robot's own flow log (`GET /api/agent/flow-logs`), "
          "so these are real turns, not synthetic ones. Sample size is however "
          "many turns that robot had.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", help="robot host, e.g. lamp-ac82.local")
    ap.add_argument("--password", help="device password (the 4 characters in the Wi-Fi name)")
    ap.add_argument("--token", help="bearer token, if you already have one")
    ap.add_argument("--date", default="", help="YYYY-MM-DD (default: today on the robot)")
    ap.add_argument("--file", help="read a downloaded flow JSONL instead of a robot")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    if args.file:
        with open(args.file) as fh:
            return report(parse_lines(fh), args.file, args.date)

    if not args.target:
        ap.error("--target or --file is required")
    token = args.token
    if not token:
        if not args.password:
            ap.error("--password (or --token) is required to read the flow log")
        token = login(args.target, args.password, args.timeout)
    return report(fetch_flow(args.target, token, args.date, args.timeout), args.target, args.date)


if __name__ == "__main__":
    sys.exit(main())
