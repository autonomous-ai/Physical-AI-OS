package hal

import "net/http"

// VolumeState is HAL /audio/volume: Volume is the raw mixer percentage and
// MaxVolume the SAFETY.md ceiling (nil when the device declares none).
type VolumeState struct {
	Volume    int  `json:"volume"`
	MaxVolume *int `json:"max_volume"`
}

// VoiceStatus is the mic part of HAL GET /voice/status.
type VoiceStatus struct {
	VoiceAvailable bool `json:"voice_available"`
	MicMuted       bool `json:"mic_muted"`
	// HWMicSwitchMuted is the physical mic switch; nil on devices without one.
	HWMicSwitchMuted *bool `json:"hw_mic_switch_muted"`
}

// GetVolumeState reads the speaker volume and its safety ceiling.
func GetVolumeState() (*VolumeState, error) {
	var out VolumeState
	if err := doJSON(http.MethodGet, "/audio/volume", nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// SetVolumeApplied sets the raw speaker volume and returns what HAL applied
// after clamping to the safety ceiling.
func SetVolumeApplied(pct int) (*VolumeState, error) {
	var out VolumeState
	if err := doJSON(http.MethodPost, "/audio/volume", map[string]int{"volume": pct}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetVoiceStatus reads the mic state.
func GetVoiceStatus() (*VoiceStatus, error) {
	var out VoiceStatus
	if err := doJSON(http.MethodGet, "/voice/status", nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// SetMicMuted mutes or unmutes the mic. Unmuting while the hardware mic
// switch is off returns a *StatusError with Code 409.
func SetMicMuted(muted bool) error {
	path := "/voice/unmute"
	if muted {
		path = "/voice/mute"
	}
	var out struct {
		Status string `json:"status"`
	}
	return doJSON(http.MethodPost, path, nil, &out)
}
