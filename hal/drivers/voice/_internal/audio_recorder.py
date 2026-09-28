"""ALSA arecord-backed input stream.

Drop-in replacement for sounddevice.InputStream that uses arecord directly.
Needed on devices where PortAudio's hw: interface bypasses ALSA's sample-rate
conversion and produces corrupted audio.
"""

import subprocess


class ArecordStream:
    """Drop-in replacement for sd.InputStream using arecord subprocess.

    Records directly via ALSA plughw which handles sample-rate conversion
    natively — the same path as `arecord -D plughw:X,0`. sounddevice uses
    PortAudio's hw: interface which bypasses ALSA SRC, producing corrupted
    audio at rates the hardware doesn't natively support.
    """

    def __init__(self, alsa_device: str, rate: int, channels: int, blocksize: int, np):
        self._device = alsa_device
        self._rate = rate
        self._channels = channels
        self._blocksize = blocksize
        self._np = np
        self._proc = None
        self._bytes_per_frame = 2 * channels  # int16 = 2 bytes

    def __enter__(self):
        # Capture stderr (don't DEVNULL it): when arecord dies, its ALSA error
        # message — device busy / USB dropout / xrun — is the only clue to the
        # root cause. read() surfaces it instead of a bare "stdout EOF".
        self._proc = subprocess.Popen(
            ["arecord", "-D", self._device, "-f", "S16_LE",
             "-r", str(self._rate), "-c", str(self._channels),
             "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return self

    def abort(self):
        """Unblock a concurrent read and reap the process before releasing ALSA."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    def __exit__(self, *args):
        self.abort()
        proc = self._proc
        if proc is not None:
            proc.stdout.close()
            proc.stderr.close()
            self._proc = None

    def read(self, frames):
        n_bytes = frames * self._bytes_per_frame
        proc = self._proc
        if proc is None:
            raise IOError("arecord stream is closed")
        raw = proc.stdout.read(n_bytes)
        if not raw:
            # arecord process died — surface its ALSA stderr + exit code so the
            # root cause (device busy / USB dropout / xrun) is visible, instead of
            # swallowing it. Raise so the main loop can restart capture.
            rc = proc.poll()
            err = ""
            try:
                if rc is not None:
                    err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            except Exception:
                pass
            raise IOError(
                f"arecord process exited (stdout EOF, rc={rc}): {err or 'no stderr'}"
            )
        if len(raw) < n_bytes:
            raw = raw + b"\x00" * (n_bytes - len(raw))
        data = self._np.frombuffer(raw, dtype=self._np.int16).reshape(frames, self._channels)
        return data, False
