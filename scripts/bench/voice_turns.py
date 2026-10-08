#!/usr/bin/env python3
"""Per-utterance voice timeline from a HAL server.log.

    python3 scripts/bench/voice_turns.py server.log            # table + p50/p95
    python3 scripts/bench/voice_turns.py server.log --json     # one row per turn

Reads only lines HAL already writes: `[voice-metrics] speech end`, `[turn-timing]`,
`[voice-metrics] ack/answer`, `[turn] route=` and `[admission]`. Nothing on the
device changes; copy the log off and run this anywhere.
"""

import argparse
import json
import re
import sys
from collections import Counter

RE_SPEECH_END = re.compile(r"\[voice-metrics\] speech end \(interaction=(?P<iid>\S+) method=(?P<method>[^)]+)\)")
RE_TIMING = re.compile(r"\[turn-timing\] interaction=(?P<iid>\S+) (?P<key>[a-z_]+_ms)=(?P<ms>-?\d+)")
RE_ACK = re.compile(r"\[voice-metrics\] (?P<kind>ack|answer) \(interaction=(?P<iid>\S+) kind=(?P<akind>\S+) latency_ms=(?P<ms>\S+)\)")
RE_ROUTE = re.compile(r"\[turn\] route=(?P<route>\S+) → (?P<dest>.*?) \(event=(?P<event>\S+), stt=(?P<stt>.*?), interaction_id=(?P<iid>\S+)\)")
RE_ADMISSION = re.compile(r"\[admission\] (?P<text>.*)")
RE_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)")

COLUMNS = [
    "speech_end_to_commit_ms",
    "commit_to_first_output_ms",
    "speech_end_to_first_speech_ms",
    "ack_latency_ms",
    "answer_latency_ms",
]


def parse(lines):
    """Return (turns ordered by first sight, admission notes)."""
    turns = {}
    order = []
    admissions = []

    def turn(iid):
        if iid not in turns:
            turns[iid] = {"interaction_id": iid}
            order.append(iid)
        return turns[iid]

    for line in lines:
        ts = RE_TS.match(line)
        if m := RE_SPEECH_END.search(line):
            t = turn(m["iid"])
            t["endpoint"] = m["method"]
            if ts:
                t["at"] = ts["ts"]
            continue
        if m := RE_TIMING.search(line):
            turn(m["iid"])[m["key"]] = int(m["ms"])
            continue
        if m := RE_ACK.search(line):
            t = turn(m["iid"])
            if m["ms"] != "None":
                t[f"{m['kind']}_latency_ms"] = int(m["ms"])
                t[f"{m['kind']}_kind"] = m["akind"]
            continue
        if m := RE_ROUTE.search(line):
            t = turn(m["iid"])
            t["route"] = m["route"]
            t["destination"] = m["dest"]
            t["transcript"] = m["stt"]
            continue
        if m := RE_ADMISSION.search(line):
            admissions.append(m["text"])
    return [turns[i] for i in order], admissions


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return int(round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)))


def summarize(turns):
    rows = []
    for col in COLUMNS:
        values = [t[col] for t in turns if isinstance(t.get(col), int)]
        rows.append((col, len(values), percentile(values, 50), percentile(values, 95), max(values) if values else None))
    return rows


def render(turns, admissions):
    out = []
    out.append(f"{len(turns)} utterance(s)")
    out.append("")
    header = f"{'interaction':<22} {'endpoint':<14} {'route':<16} {'end→commit':>10} {'commit→out':>10} {'end→speech':>10} {'ack':>7} {'answer':>7}  transcript"
    out.append(header)
    out.append("-" * len(header))
    for t in turns:
        def cell(key):
            v = t.get(key)
            return f"{v:>10}" if isinstance(v, int) else f"{'-':>10}"
        out.append(
            f"{t['interaction_id']:<22} {t.get('endpoint', '-'):<14} {t.get('route', '-'):<16} "
            f"{cell('speech_end_to_commit_ms')} {cell('commit_to_first_output_ms')} {cell('speech_end_to_first_speech_ms')} "
            f"{str(t.get('ack_latency_ms', '-')):>7} {str(t.get('answer_latency_ms', '-')):>7}  {t.get('transcript', '')[:50]}"
        )
    out.append("")
    out.append(f"{'stage':<32} {'n':>4} {'p50':>7} {'p95':>7} {'max':>7}")
    for col, n, p50, p95, mx in summarize(turns):
        out.append(f"{col:<32} {n:>4} {str(p50):>7} {str(p95):>7} {str(mx):>7}")
    routes = Counter(t.get("route", "(none)") for t in turns)
    out.append("")
    out.append("routes: " + ", ".join(f"{k}={v}" for k, v in routes.most_common()))
    if admissions:
        out.append(f"admission notes: {len(admissions)}")
        for text in admissions[:10]:
            out.append("  " + text)
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="HAL server.log (or - for stdin)")
    ap.add_argument("--json", action="store_true", help="print one JSON object per utterance")
    args = ap.parse_args(argv)
    source = sys.stdin if args.log == "-" else open(args.log, encoding="utf-8", errors="replace")
    with source:
        turns, admissions = parse(source)
    if args.json:
        for t in turns:
            print(json.dumps(t, ensure_ascii=False))
        return 0
    print(render(turns, admissions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
