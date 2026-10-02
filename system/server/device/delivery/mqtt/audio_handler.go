package mqtthandler

import (
	"encoding/json"
	"errors"
	"log/slog"
	"math"
	"net/http"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
)

// The volume.* and mic.* kinds are the MQTT twin of the web Overview Audio
// card: same HAL calls, and volume uses the slider's 0-100% of the allowed
// range (SAFETY.md max_volume) rather than raw mixer percentages.

const errNoAudio = "this device has no audio"

// errHWMicSwitch replaces HAL's 409 so the app can show it verbatim.
const errHWMicSwitch = "Hardware mic switch is off — flip the physical switch to unmute"

func (h *DeviceMQTTHandler) hasAudio() bool {
	return device.Has(h.config.DeviceTypeOrDefault(), device.CapAudio)
}

// volumeShare converts HAL's state into the slider's view; ceiling 0 reads as 0%.
func volumeShare(s *hal.VolumeState) domain.MQTTVolumeState {
	ceiling := 100
	if s.MaxVolume != nil {
		ceiling = *s.MaxVolume
	}
	share := 0
	if ceiling > 0 {
		share = int(math.Round(float64(s.Volume) * 100 / float64(ceiling)))
		share = max(0, min(100, share))
	}
	return domain.MQTTVolumeState{Volume: share, Raw: s.Volume, MaxVolume: ceiling}
}

// handleVolumeGet handles kind="volume.get". Synchronous.
func (h *DeviceMQTTHandler) handleVolumeGet(env domain.MQTTDataCommand) error {
	if !h.hasAudio() {
		return h.publishDataResult(domain.KindVolumeGet, "failure", errNoAudio, nil)
	}
	s, err := hal.GetVolumeState()
	if err != nil {
		slog.Error("volume.get: failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindVolumeGet, "failure", err.Error(), nil)
	}
	return h.publishDataResult(domain.KindVolumeGet, "success", "", volumeShare(s))
}

// handleVolumeSet handles kind="volume.set" — the web slider's release:
// the share is mapped onto the allowed range, then HAL clamps and persists it.
// Synchronous.
func (h *DeviceMQTTHandler) handleVolumeSet(env domain.MQTTDataCommand) error {
	var d domain.MQTTVolumeData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindVolumeSet, "failure", "invalid volume.set data: "+err.Error(), nil)
	}
	if d.Volume == nil || *d.Volume < 0 || *d.Volume > 100 {
		return h.publishDataResult(domain.KindVolumeSet, "failure", "volume must be 0-100", nil)
	}
	if !h.hasAudio() {
		return h.publishDataResult(domain.KindVolumeSet, "failure", errNoAudio, nil)
	}
	cur, err := hal.GetVolumeState()
	if err != nil {
		slog.Error("volume.set: read ceiling failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindVolumeSet, "failure", err.Error(), nil)
	}
	ceiling := volumeShare(cur).MaxVolume
	raw := int(math.Round(float64(*d.Volume) * float64(ceiling) / 100))
	applied, err := hal.SetVolumeApplied(raw)
	if err != nil {
		slog.Error("volume.set: failed", "component", "mqtt", "raw", raw, "error", err)
		return h.publishDataResult(domain.KindVolumeSet, "failure", err.Error(), nil)
	}
	slog.Info("volume.set: applied", "component", "mqtt", "share", *d.Volume, "raw", applied.Volume)
	return h.publishDataResult(domain.KindVolumeSet, "success", "", volumeShare(applied))
}

func (h *DeviceMQTTHandler) micState() (*domain.MQTTMicState, error) {
	s, err := hal.GetVoiceStatus()
	if err != nil {
		return nil, err
	}
	return &domain.MQTTMicState{Muted: s.MicMuted, HWSwitchMuted: s.HWMicSwitchMuted, Available: s.VoiceAvailable}, nil
}

// handleMicGet handles kind="mic.get". Synchronous.
func (h *DeviceMQTTHandler) handleMicGet(env domain.MQTTDataCommand) error {
	if !h.hasAudio() {
		return h.publishDataResult(domain.KindMicGet, "failure", errNoAudio, nil)
	}
	st, err := h.micState()
	if err != nil {
		slog.Error("mic.get: failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindMicGet, "failure", err.Error(), nil)
	}
	return h.publishDataResult(domain.KindMicGet, "success", "", st)
}

// handleMicSet handles kind="mic.set" — the web Mute/Unmute button. The
// hardware mic switch wins: unmuting while it is off fails. Synchronous.
func (h *DeviceMQTTHandler) handleMicSet(env domain.MQTTDataCommand) error {
	var d domain.MQTTMicData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindMicSet, "failure", "invalid mic.set data: "+err.Error(), nil)
	}
	if d.Muted == nil {
		return h.publishDataResult(domain.KindMicSet, "failure", "muted is required", nil)
	}
	if !h.hasAudio() {
		return h.publishDataResult(domain.KindMicSet, "failure", errNoAudio, nil)
	}
	if err := hal.SetMicMuted(*d.Muted); err != nil {
		var se *hal.StatusError
		if errors.As(err, &se) && se.Code == http.StatusConflict {
			return h.publishDataResult(domain.KindMicSet, "failure", errHWMicSwitch, nil)
		}
		slog.Error("mic.set: failed", "component", "mqtt", "muted", *d.Muted, "error", err)
		return h.publishDataResult(domain.KindMicSet, "failure", err.Error(), nil)
	}
	slog.Info("mic.set: applied", "component", "mqtt", "muted", *d.Muted)
	st, err := h.micState()
	if err != nil {
		// The change went through; report what was asked for.
		st = &domain.MQTTMicState{Muted: *d.Muted}
	}
	return h.publishDataResult(domain.KindMicSet, "success", "", st)
}
