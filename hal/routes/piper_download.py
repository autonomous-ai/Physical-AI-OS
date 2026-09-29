"""Piper download worker — runs outside HAL, as its own transient unit.

A voice is 63 MB: minutes on a domestic connection. HAL restarts for reasons
that have nothing to do with the download — saving *any* voice setting makes
os-server run `systemctl restart hal`, and hal.service is KillMode=control-group,
so a thread or an ordinary child process dies with it. When that happened the
transfer stopped and the in-memory job record vanished with it, so the admin
page reverted to "Download 63 MB" as though the click had never happened: no
error, no partial file, nothing to retry from.

So the download lives outside that cgroup, and the two sides agree through a
job file instead of shared memory. HAL can restart as often as it likes; the
transfer keeps running and the page keeps showing it.

Invoked as a plain script, never imported: `python3 piper_download.py <job-file>
<spec-json>`. It deliberately imports nothing from `hal` — the package pulls in
hardware drivers on import, which is not something a downloader should be
touching, and keeping it dependency-free means it survives HAL being broken.

Spec shape:
    {"kind": "voice", "target": "<name>",
     "steps": [{"url": ..., "dest": ..., "from": 0, "to": 3, "track": false}, ...]}
    {"kind": "engine", "target": "piper",
     "url": ..., "dir": "/opt/piper", "voices_dir": "/opt/piper/voices"}
"""

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request

CHUNK = 256 * 1024

# The job file is on the SD card, so progress is not written per chunk: 63 MB in
# 256 KB chunks would be ~250 writes for a single download, and these boards die
# of write wear. Two seconds also happens to be how often the admin page polls,
# so a finer granularity would not reach anyone anyway.
WRITE_INTERVAL_S = 2.0

_job_path = ""
_state: dict = {}
_last_write = 0.0


def _record(**over) -> dict:
    rec = dict(_state)
    rec.update(over)
    rec["pid"] = os.getpid()
    return rec


def _flush(force: bool = False, **over) -> None:
    """Write the job file atomically, throttled unless forced.

    Every writer emits a complete record rather than patching fields, so HAL's
    initial claim and this process's updates can never interleave into a
    half-updated state — the newest complete write simply wins.
    """
    global _last_write
    _state.update(over)
    now = time.time()
    if not force and now - _last_write < WRITE_INTERVAL_S:
        return
    _last_write = now
    tmp = _job_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_record(), fh)
        os.replace(tmp, _job_path)
    except OSError:
        pass


def _download(url: str, dest: str, w_from: int, w_to: int, track: bool) -> None:
    """Fetch url to dest, mapping its progress onto a slice of the whole job.

    Writes to a .part file and renames on success, so an interrupted transfer
    can never leave a truncated .onnx that Piper would later fail to load in a
    way that looks like a corrupt install.
    """
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        if track:
            _flush(force=True, bytes_total=total, bytes_done=0)
        got = 0
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            over = {"bytes_done": got} if track else {}
            if total:
                over["percent"] = w_from + int((w_to - w_from) * got / total)
            _flush(**over)
    os.replace(tmp, dest)


def _discard(paths) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def _run_voice(spec: dict) -> None:
    steps = spec["steps"]
    os.makedirs(os.path.dirname(steps[0]["dest"]), exist_ok=True)
    try:
        for step in steps:
            _download(step["url"], step["dest"], step["from"], step["to"],
                      step.get("track", False))
    except Exception:
        # A half-installed voice is worse than none: the listing keys off the
        # .onnx, so a stranded sidecar is invisible in the UI while still
        # occupying space, and a later retry would find it already in place.
        _discard([p for s in steps for p in (s["dest"], s["dest"] + ".part")])
        raise


def _extract_engine(tar_path: str, destination: str) -> Path:
    """Extract release files without allowing archive paths to escape Piper."""
    root = Path(destination).resolve()
    source = root / "piper"

    def release_filter(member: tarfile.TarInfo, path: str):
        name = PurePosixPath(member.name)
        if (name.is_absolute() or ".." in name.parts
                or not name.parts or name.parts[0] != "piper"):
            raise ValueError(f"Unsafe Piper archive path: {member.name}")
        filtered = tarfile.data_filter(member, path)
        target = root / member.name
        if not target.resolve().is_relative_to(source):
            raise ValueError(f"Piper archive path escapes release: {member.name}")
        if member.issym() or member.islnk():
            link = Path(member.linkname)
            base = target.parent if member.issym() else root
            if link.is_absolute() or not (base / link).resolve().is_relative_to(source):
                raise ValueError(f"Unsafe Piper archive link: {member.name}")
        return filtered

    with tarfile.open(tar_path) as tf:
        members = tf.getmembers()
        # Check the complete manifest first, then recheck while extracting so
        # earlier symlinks cannot redirect later writes outside the release.
        for member in members:
            release_filter(member, destination)
        tf.extractall(destination, members=members, filter=release_filter)
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Piper archive is missing its release directory")
    for entry in source.rglob("*"):
        if not entry.resolve().is_relative_to(source):
            raise ValueError(f"Piper archive link escapes release: {entry.name}")
        # copytree follows links. Reject directory links (including cycles),
        # while retaining shared-library file symlinks used by real releases.
        if entry.is_symlink() and not entry.is_file():
            raise ValueError(f"Invalid Piper archive file link: {entry.name}")
    return source


def _run_engine(spec: dict) -> None:
    piper_dir = spec["dir"]
    os.makedirs(piper_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tar_path = os.path.join(td, "piper.tar.gz")
        _download(spec["url"], tar_path, 0, 85, True)
        _flush(force=True, percent=88)
        src = _extract_engine(tar_path, os.path.join(td, "extracted"))
        for entry in os.listdir(src):
            s, d = os.path.join(src, entry), os.path.join(piper_dir, entry)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    os.chmod(os.path.join(piper_dir, "piper"), 0o755)
    os.makedirs(spec["voices_dir"], exist_ok=True)


def main() -> int:
    global _job_path
    _job_path, spec_raw = sys.argv[1], sys.argv[2]
    spec = json.loads(spec_raw)
    _state.update(
        active=True, kind=spec["kind"], target=spec["target"], percent=0,
        bytes_done=0, bytes_total=0, error="", done=False, claimed_at=time.time(),
    )
    _flush(force=True)
    try:
        if spec["kind"] == "engine":
            _run_engine(spec)
        else:
            _run_voice(spec)
    except Exception as e:
        _flush(force=True, active=False, done=True, error=str(e))
        return 1
    _flush(force=True, active=False, done=True, percent=100, error="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
