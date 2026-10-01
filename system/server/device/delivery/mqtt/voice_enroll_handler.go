package mqtthandler

import (
	"encoding/base64"
	"encoding/json"
	"log/slog"
	"path/filepath"
	"strings"
	"sync"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/voicefile"
)

// The voice.* kinds are the MQTT twin of the web Voice settings: same HAL
// calls, same os-server file rules, same 15-second recording on the lamp mic.

// voiceEnrollSec matches the web Voice settings' VOICE_DURATION_SEC.
const voiceEnrollSec = 15

// voiceLabelMaxLen matches HAL's folder-label cap.
const voiceLabelMaxLen = 64

// voiceFileMaxBytes caps a sample carried inline; a 15s 16kHz mono WAV is ~480 KB.
const voiceFileMaxBytes = 2 << 20

// voiceUsersDir is swapped in tests.
var voiceUsersDir = voicefile.UsersDir

// voiceMu serializes voice mutations: HAL rewrites the profile's embeddings
// and metadata.json without a lock, so enroll and removals must not overlap.
var voiceMu sync.Mutex

// voiceMIME mirrors the content types HAL GET /face/file serves for voice files.
var voiceMIME = map[string]string{
	".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".webm": "audio/webm",
	".json": "application/json", ".jsonl": "application/json", ".npy": "application/octet-stream",
}

// handleVoiceEnroll handles kind="voice.enroll" — the web's "Start Recording":
// HAL POST /speaker/record-enroll records 15s from the lamp's own mic, never
// phone audio. Acks `starting` with the duration when recording can begin,
// then records off the MQTT callback.
func (h *DeviceMQTTHandler) handleVoiceEnroll(env domain.MQTTDataCommand) error {
	var d domain.MQTTVoiceData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindVoiceEnroll, "failure", "invalid voice.enroll data: "+err.Error(), nil)
	}
	label, errMsg := validVoiceLabel(d.Label)
	if errMsg != "" {
		slog.Warn("voice.enroll: rejected", "component", "mqtt", "error", errMsg)
		return h.publishDataResult(domain.KindVoiceEnroll, "failure", errMsg, nil)
	}

	slog.Info("voice.enroll: received", "component", "mqtt", "label", label)
	go func() {
		// Hold the lock before acking so the countdown starts when recording can.
		voiceMu.Lock()
		starting := domain.MQTTVoiceEnrollStarting{Label: label, DurationSec: voiceEnrollSec}
		if err := h.publishDataResult(domain.KindVoiceEnroll, "starting", "", starting); err != nil {
			slog.Warn("voice.enroll: publish starting ack failed", "component", "mqtt", "error", err)
		}
		res, err := hal.VoiceRecordEnroll(hal.VoiceRecordEnrollRequest{Name: label, DurationSec: voiceEnrollSec})
		voiceMu.Unlock()
		if err != nil {
			slog.Error("voice.enroll: failed", "component", "mqtt", "label", label, "error", err)
			if pubErr := h.publishDataResult(domain.KindVoiceEnroll, "failure", err.Error(), nil); pubErr != nil {
				slog.Warn("voice.enroll: publish failure ack failed", "component", "mqtt", "error", pubErr)
			}
			return
		}
		slog.Info("voice.enroll: success", "component", "mqtt", "label", res.Meta.Name, "num_samples", res.Meta.NumSamples)
		if pubErr := h.publishDataResult(domain.KindVoiceEnroll, "success", "", res.Meta); pubErr != nil {
			slog.Warn("voice.enroll: publish success ack failed", "component", "mqtt", "error", pubErr)
		}
	}()
	return nil
}

// validVoiceLabel trims and lowercases a person label as the web does; errMsg is "" on success.
func validVoiceLabel(raw string) (string, string) {
	label := voicefile.NormalizeName(raw)
	if label == "" {
		return "", "label is required"
	}
	if len([]rune(label)) > voiceLabelMaxLen {
		return "", "label exceeds 64 characters"
	}
	return label, ""
}

// handleVoiceOwners handles kind="voice.owners" — the web's "Voice Files"
// list: everyone with files in users/<label>/voice/, with the file names.
// Synchronous: a directory scan.
func (h *DeviceMQTTHandler) handleVoiceOwners(env domain.MQTTDataCommand) error {
	owners, err := voicefile.List(voiceUsersDir)
	if err != nil {
		slog.Error("voice.owners: failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindVoiceOwners, "failure", err.Error(), nil)
	}
	return h.publishDataResult(domain.KindVoiceOwners, "success", "", map[string]any{"persons": owners})
}

// handleVoiceRemove handles kind="voice.remove" — the web's "Remove all":
// HAL POST /speaker/remove deletes the voice profile only. Acks `starting`,
// then runs off the MQTT callback since it may wait on a recording's lock.
func (h *DeviceMQTTHandler) handleVoiceRemove(env domain.MQTTDataCommand) error {
	var d domain.MQTTVoiceData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindVoiceRemove, "failure", "invalid voice.remove data: "+err.Error(), nil)
	}
	label, errMsg := validVoiceLabel(d.Label)
	if errMsg != "" {
		return h.publishDataResult(domain.KindVoiceRemove, "failure", errMsg, nil)
	}

	slog.Info("voice.remove: received", "component", "mqtt", "label", label)
	if err := h.publishDataResult(domain.KindVoiceRemove, "starting", "", nil); err != nil {
		slog.Warn("voice.remove: publish starting ack failed", "component", "mqtt", "error", err)
	}
	go func() {
		voiceMu.Lock()
		res, err := hal.VoiceRemove(label)
		voiceMu.Unlock()
		if err != nil {
			slog.Error("voice.remove: failed", "component", "mqtt", "label", label, "error", err)
			if pubErr := h.publishDataResult(domain.KindVoiceRemove, "failure", err.Error(), nil); pubErr != nil {
				slog.Warn("voice.remove: publish failure ack failed", "component", "mqtt", "error", pubErr)
			}
			return
		}
		slog.Info("voice.remove: success", "component", "mqtt", "label", res.Name)
		if pubErr := h.publishDataResult(domain.KindVoiceRemove, "success", "", res); pubErr != nil {
			slog.Warn("voice.remove: publish success ack failed", "component", "mqtt", "error", pubErr)
		}
	}()
	return nil
}

// handleVoiceFileGet handles kind="voice.file.get" — the web's per-sample
// player: returns one file from users/<label>/voice/ as base64. Synchronous.
func (h *DeviceMQTTHandler) handleVoiceFileGet(env domain.MQTTDataCommand) error {
	var d domain.MQTTVoiceFileData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindVoiceFileGet, "failure", "invalid voice.file.get data: "+err.Error(), nil)
	}
	label, file := voicefile.NormalizeName(d.Label), strings.TrimSpace(d.File)
	data, err := voicefile.Read(voiceUsersDir, label, file, voiceFileMaxBytes)
	if err != nil {
		slog.Info("voice.file.get: refused", "component", "mqtt", "label", label, "file", file, "reason", err)
		return h.publishDataResult(domain.KindVoiceFileGet, "failure", err.Error(), nil)
	}
	contentType := voiceMIME[strings.ToLower(filepath.Ext(file))]
	if contentType == "" {
		contentType = "text/plain"
	}
	return h.publishDataResult(domain.KindVoiceFileGet, "success", "", domain.MQTTVoiceFileContent{
		Label:         label,
		File:          file,
		ContentType:   contentType,
		Size:          len(data),
		ContentBase64: base64.StdEncoding.EncodeToString(data),
	})
}

// handleVoiceFileRemove handles kind="voice.file.remove" — the web's per-sample
// "×" (POST /api/voice/file/remove): deletes one audio sample and its .npy,
// and drops the whole profile when it was the last WAV. Synchronous.
func (h *DeviceMQTTHandler) handleVoiceFileRemove(env domain.MQTTDataCommand) error {
	var d domain.MQTTVoiceFileData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindVoiceFileRemove, "failure", "invalid voice.file.remove data: "+err.Error(), nil)
	}
	if !voiceMu.TryLock() {
		return h.publishDataResult(domain.KindVoiceFileRemove, "failure", "voice enrollment in progress — try again", nil)
	}
	res, err := voicefile.Remove(voiceUsersDir, d.Label, d.File)
	voiceMu.Unlock()
	if err != nil {
		slog.Info("voice.file.remove: refused", "component", "mqtt", "label", d.Label, "file", d.File, "reason", err)
		return h.publishDataResult(domain.KindVoiceFileRemove, "failure", err.Error(), nil)
	}
	return h.publishDataResult(domain.KindVoiceFileRemove, "success", "", res)
}
