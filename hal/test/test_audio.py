import sounddevice as sd
import numpy as np

OUTPUT_NAMES = ["seeed", "cd002"]
INPUT_NAMES = ["seeed", "webcam"]


def get_audio_device(output=True):
    """Return the first matching audio device index for output or input."""
    names = OUTPUT_NAMES if output else INPUT_NAMES
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].lower()
        if output and d["max_output_channels"] > 0 and any(k in name for k in names):
            return i
        if not output and d["max_input_channels"] > 0 and any(k in name for k in names):
            return i
    return None


audio_output = get_audio_device(output=True)
audio_input = get_audio_device(output=False)

if audio_output is None:
    # Hardware script, not a unit test: skip at collection when no device is present.
    try:
        import pytest

        pytest.skip(
            "no %s output device — audio smoke test needs a real body" % OUTPUT_NAMES,
            allow_module_level=True,
        )
    except ImportError:
        raise RuntimeError(
            "No output audio device found! (looked for: %s)" % OUTPUT_NAMES
        )

dev_info = sd.query_devices(audio_output)
sample_rate = int(dev_info["default_samplerate"])
print(f"Output device: {dev_info['name']} (rate={sample_rate})")

duration = 3
print("Playing test tone...")
frequency = 440
t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
tone = 0.5 * np.sin(2 * np.pi * frequency * t).astype(np.float32)
sd.play(tone, samplerate=sample_rate, device=audio_output)
sd.wait()

if audio_input is not None:
    in_info = sd.query_devices(audio_input)
    in_rate = int(in_info["default_samplerate"])
    print(f"Input device: {in_info['name']} (rate={in_rate})")
    print("Recording from microphone...")
    recording = sd.rec(int(duration * in_rate), samplerate=in_rate,
                       channels=1, device=audio_input)
    sd.wait()
    print("Recording complete.")

    print("Playing back recorded audio...")
    sd.play(recording, samplerate=sample_rate, device=audio_output)
    sd.wait()
else:
    print("No input device found (mic may use ALSA default), skipping record test.")

print("Done.")
