# Hardware microphone switch

The hardware microphone mute switch blocks voice startup and scene microphone activation even on devices without camera or speaker privacy overlays. `privacy.mic_locked()` checks the hardware state directly. Software unmute still cannot override a closed hardware switch.
