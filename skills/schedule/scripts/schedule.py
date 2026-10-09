#!/usr/bin/env python3
"""Read the device's scheduled tasks — the ones shown in the app and fired by the OS.

Read-only. It reads the task files the OS keeps next to config.json, so it
needs no credential and never touches config.json itself.

Usage:
  schedule.py list
  schedule.py show <id>

`list` prints the timezone, a count, and one block per task: name, id, kind,
cadence, connectors it needs, last run and next run (already includes this
device's start offset). `show` prints one task in full, including its
instructions.

Exit codes: 0 success · 1 unreadable data · 2 usage error · 3 unknown id.
On any failure nothing is written to stdout.
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

DEFAULT_CONFIG_PATH = "/root/config/config.json"
WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def state_dir():
    return Path(os.environ.get("OS_CONFIG_PATH", DEFAULT_CONFIG_PATH)).parent


def read_json(path, default):
    """A missing file is an empty list of tasks; a file that exists but cannot be parsed is an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as e:
        raise Failure(1, f"cannot read {path.name}: {e.strerror or type(e).__name__}")
    try:
        return json.loads(text)
    except ValueError:
        raise Failure(1, f"{path.name} is not valid JSON")


def load():
    data = read_json(state_dir() / "schedules.json", {})
    intents = read_json(state_dir() / "schedule-intents.json", {})
    if not isinstance(data, dict) or not isinstance(intents, dict):
        raise Failure(1, "unexpected schedule file layout")
    return data.get("timezone") or "", data.get("schedules") or [], intents.get("intents") or []


def zone_of(name):
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return dt.timezone.utc


def parse_time(raw):
    if not raw or str(raw).startswith("0001-"):
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_time(raw, zone):
    t = parse_time(raw)
    return t.astimezone(zone).strftime("%Y-%m-%d %H:%M") if t else ""


def day_name(n):
    try:
        return WEEKDAYS[int(n) % 7]
    except (TypeError, ValueError):
        return str(n)


def fmt_interval(ms):
    try:
        seconds = int(ms) // 1000
    except (TypeError, ValueError):
        return "?"
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{seconds // 3600} h"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def cadence_text(spec, zone):
    spec = spec or {}
    repeat = spec.get("repeat") or "?"
    times = spec.get("times") or ([spec["time"]] if spec.get("time") else [])
    at = ", ".join(times)
    if repeat == "daily":
        return f"daily at {at}" if at else "daily"
    if repeat == "weekly":
        days = ", ".join(day_name(d) for d in spec.get("days") or [])
        return f"weekly on {days} at {at}".strip()
    if repeat == "monthly":
        return f"monthly on day {spec.get('day_of_month', '?')} at {at}".strip()
    if repeat == "interval":
        return f"every {fmt_interval(spec.get('every_ms'))}"
    if repeat == "once":
        return f"once at {fmt_time(spec.get('at'), zone)}"
    if repeat == "manual":
        return "manual (runs only when asked)"
    return repeat


def pending_by_id(intents):
    pending = {}
    for it in intents:
        if it.get("op") in ("update", "delete") and it.get("schedule_id"):
            pending[it["schedule_id"]] = it["op"]
    return pending


def block(s, zone, pending_op=""):
    kind = s.get("kind") or "agent"
    state = "enabled" if s.get("enabled") else "disabled"
    head = f"- {s.get('name') or '(unnamed)'}  [id {s.get('id', '?')}]  {state}, {kind}"
    lines = [head, f"    cadence: {cadence_text(s.get('schedule'), zone)}"]
    if s.get("requires"):
        lines.append(f"    needs connectors: {', '.join(s['requires'])}")
    if s.get("end_at"):
        lines.append(f"    ends: {fmt_time(s['end_at'], zone)}")
    last = fmt_time(s.get("last_run_at"), zone)
    if last:
        status = s.get("last_run_status") or "?"
        summary = s.get("last_run_summary") or ""
        extra = f" ({summary})" if summary and status != "success" else ""
        lines.append(f"    last run: {last} {status}{extra}")
    else:
        lines.append("    last run: never")
    nxt = fmt_time(s.get("next_run_at"), zone)
    if s.get("enabled"):
        lines.append(f"    next run: {nxt + ' ' + str(zone) if nxt else 'none scheduled'}")
    if pending_op:
        lines.append(f"    pending: a {pending_op} was requested on the device and is not confirmed yet")
    return lines


def cmd_list():
    tz_name, schedules, intents = load()
    zone = zone_of(tz_name)
    pending = pending_by_id(intents)
    enabled = sum(1 for s in schedules if s.get("enabled"))
    now = dt.datetime.now(zone).strftime("%Y-%m-%d %H:%M")
    out = [f"Timezone: {tz_name or 'unknown (showing UTC)'}; now {now}"]
    out.append(f"Scheduled tasks: {len(schedules)} ({enabled} enabled, {len(schedules) - enabled} disabled)")
    for s in schedules:
        out.extend(block(s, zone, pending.get(s.get("id"), "")))
    creates = [it for it in intents if it.get("op") == "create"]
    for it in creates:
        p = it.get("schedule") or {}
        out.append(f"- {p.get('name') or '(unnamed)'}  [not confirmed yet]  {cadence_text(p.get('schedule'), zone)}")
    if creates:
        out.append(f"{len(creates)} new task(s) above are pending confirmation and will not run until confirmed.")
    out.append("Not included: recurring jobs the agent runtime created with its own scheduler. "
               "Those do not appear in the app and are not counted here.")
    print("\n".join(out))
    return 0


def cmd_show(task_id):
    tz_name, schedules, intents = load()
    zone = zone_of(tz_name)
    for s in schedules:
        if s.get("id") == task_id:
            out = block(s, zone, pending_by_id(intents).get(task_id, ""))
            out.append(f"    instructions: {s.get('instructions') or ''}")
            print("\n".join(out))
            return 0
    raise Failure(3, f"no scheduled task with id {task_id}")


def main(argv):
    try:
        if argv == ["list"]:
            return cmd_list()
        if len(argv) == 2 and argv[0] == "show":
            return cmd_show(argv[1])
        print(__doc__.split("\n\n", 1)[1], file=sys.stderr)
        return 2
    except Failure as e:
        print(str(e), file=sys.stderr)
        return e.code
    except BrokenPipeError:
        raise
    except Exception as e:
        print(f"schedule helper failed: {type(e).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 1
    sys.exit(code)
