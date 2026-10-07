"""Voice input routing and per-turn policy, independent of audio streaming.

Automatic and explicit captures share the recorder/STT pipeline. This boundary
owns the differences in wake, realtime, dispatch and visual feedback; the shared
capture controller only owns synchronization and accepts a route matcher.
"""

from dataclasses import dataclass

from hal import config, presets
from hal.drivers.voice._internal.harness_capture import same_target
from hal.drivers.voice._internal.harness_voice import bypass_realtime


def device_manual_mode(snapshot):
    """Device taps require an authoritative Harness-off routing generation."""
    return (
        config.VOICE_INPUT_MODE == "tap_to_talk"
        and snapshot.get("enabled") is False
        and not snapshot.get("unavailable", False)
        and type(snapshot.get("generation")) is int
        and snapshot["generation"] >= 0
    )


def device_snapshot(snapshot):
    """Bind an explicit device capture to the current routing generation."""
    return dict(snapshot, deviceInputMode="tap_to_talk")


def requires_manual_capture(snapshot):
    return bypass_realtime(snapshot) or device_manual_mode(snapshot)


def same_capture_target(left, right):
    """Never move recorded speech between devices, modes or Harness targets."""
    if left.get("deviceInputMode") == "tap_to_talk":
        return (device_manual_mode(right)
                and left.get("generation") == right.get("generation"))
    return same_target(left, right)


@dataclass(frozen=True)
class InputPolicy:
    """Freeze capture behavior for one turn; route ownership is checked live."""

    explicit: bool
    device_capture: bool
    harness_capture: bool
    realtime_allowed: bool

    @classmethod
    def for_turn(cls, snapshot, capture, *, live_mode):
        explicit = capture is not None
        return cls(
            explicit=explicit,
            device_capture=explicit and snapshot.get("deviceInputMode") == "tap_to_talk",
            harness_capture=snapshot["enabled"] and not snapshot.get("unavailable", False),
            realtime_allowed=not explicit and not live_mode and not bypass_realtime(snapshot),
        )

    @property
    def automatic(self):
        """Only automatic turns may open follow-ups, backchannels or live audio."""
        return not self.explicit

    @property
    def dispatch_directly(self):
        return self.device_capture or self.harness_capture

    def event_type_override(self, *, followup):
        """A physical capture is addressed speech even without a wake phrase.

        The OS treats plain ``voice`` as overheard ambient input. Reuse its
        existing direct-command contract so an explicit tap cannot become ambient.
        """
        if self.device_capture:
            return "voice_command"
        return "voice_followup" if followup else None

    def set_capturing(self, active, set_emotion):
        """Keep device feedback separate from Harness-owned LED state."""
        if not self.explicit:
            return
        if self.device_capture:
            if active:
                set_emotion(presets.EMO_LISTENING)
            else:
                from hal import app_state
                app_state.clear_listening_cue()
        else:
            from hal.drivers.harness.led import set_capturing
            set_capturing(active)
