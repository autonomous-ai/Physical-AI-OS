"""Release archives cannot write or copy files outside their extraction tree."""

import importlib.util
import io
from pathlib import Path
import shutil
import tarfile

import pytest


_spec = importlib.util.spec_from_file_location(
    "piper_download_security", Path(__file__).parents[1] / "routes/piper_download.py",
)
piper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(piper)


def _file(name, content=b"release"):
    entry = tarfile.TarInfo(name)
    entry.size = len(content)
    return entry, content


def _link(name, target, kind=tarfile.SYMTYPE):
    entry = tarfile.TarInfo(name)
    entry.type = kind
    entry.linkname = target
    return entry, b""


def _archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for member, contents in entries:
            archive.addfile(member, io.BytesIO(contents))
    return path


def test_engine_install_accepts_release_layout_and_shared_library_link(tmp_path, monkeypatch):
    archive = _archive(tmp_path / "release.tar.gz", [
        _file("piper/piper", b"executable"),
        _file("piper/lib/libvoice.so.1", b"library"),
        _link("piper/lib/libvoice.so", "libvoice.so.1"),
        _file("piper/espeak-ng-data/voices/en", b"voice"),
    ])
    monkeypatch.setattr(piper, "_download", lambda url, dest, *args: shutil.copy2(archive, dest))
    monkeypatch.setattr(piper, "_flush", lambda **kwargs: None)
    install = tmp_path / "installed"
    piper._run_engine({"url": "unused", "dir": str(install), "voices_dir": str(install / "voices")})
    assert (install / "piper").read_bytes() == b"executable"
    assert (install / "piper").stat().st_mode & 0o111
    assert (install / "lib/libvoice.so").read_bytes() == b"library"
    assert (install / "espeak-ng-data/voices/en").read_bytes() == b"voice"
    assert (install / "voices").is_dir()


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "hardlink", "absolute_link", "device", "fifo", "symlink_parent"])
def test_malicious_archive_rejected_without_outside_write(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    entries = [_file("piper/piper")]
    if kind == "traversal":
        entries.append(_file("../../outside", b"overwritten"))
    elif kind == "absolute":
        entries.append(_file(str(outside), b"overwritten"))
    elif kind == "symlink":
        entries.append(_link("piper/libevil.so", "../../outside"))
    elif kind == "hardlink":
        entries.append(_link("piper/libevil.so", "../outside", tarfile.LNKTYPE))
    elif kind == "absolute_link":
        entries.append(_link("piper/libevil.so", str(outside)))
    elif kind in ("device", "fifo"):
        entry = tarfile.TarInfo("piper/special")
        entry.type = tarfile.CHRTYPE if kind == "device" else tarfile.FIFOTYPE
        entries.append((entry, b""))
    else:
        entries.extend([_link("piper/escape", "../.."), _file("piper/escape/outside", b"overwritten")])
    archive = _archive(tmp_path / "evil.tar.gz", entries)
    destination = tmp_path / "extracted"
    with pytest.raises((ValueError, tarfile.FilterError)):
        piper._extract_engine(str(archive), str(destination))
    assert outside.read_bytes() == b"unchanged"
    assert not destination.exists()  # The complete manifest is checked first.


def test_directory_symlink_cycle_rejected_before_install_copy(tmp_path):
    archive = _archive(tmp_path / "cycle.tar.gz", [
        _file("piper/piper"), _link("piper/loop", "."),
    ])
    with pytest.raises(ValueError, match="file link"):
        piper._extract_engine(str(archive), str(tmp_path / "extracted"))


def test_contained_hardlink_and_relative_parent_file_link_are_allowed(tmp_path):
    archive = _archive(tmp_path / "links.tar.gz", [
        _file("piper/piper"),
        _file("piper/lib.so", b"library"),
        _link("piper/hard.so", "piper/lib.so", tarfile.LNKTYPE),
        _link("piper/lib/shared.so", "../lib.so"),
    ])
    source = piper._extract_engine(str(archive), str(tmp_path / "extracted"))
    assert (source / "hard.so").read_bytes() == b"library"
    assert (source / "lib/shared.so").read_bytes() == b"library"


def test_chained_symlink_cannot_redirect_write_outside_release(tmp_path):
    archive = _archive(tmp_path / "chain.tar.gz", [
        _file("piper/piper"),
        _link("piper/a", "b/.."),
        _link("piper/b", "."),
        _file("piper/a/escaped", b"bad"),
    ])
    destination = tmp_path / "extracted"
    with pytest.raises((ValueError, tarfile.FilterError)):
        piper._extract_engine(str(archive), str(destination))
    assert not (destination / "escaped").exists()
