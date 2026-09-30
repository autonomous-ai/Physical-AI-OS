package mqtthandler

import (
	"encoding/base64"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
)

// faceEnrollMaxBytes caps the decoded photo; HAL itself has no limit.
const faceEnrollMaxBytes = 10 << 20

// faceEnrollLabelMaxLen matches HAL FaceEnrollRequest.label max_length.
const faceEnrollLabelMaxLen = 64

// faceEnrollMu serializes face.enroll and face.remove: HAL rewrites the
// user's metadata.json and retrains without a lock, so they must not overlap.
var faceEnrollMu sync.Mutex

// handleFaceEnroll handles kind="face.enroll" — the MQTT counterpart of
// HAL POST /face/enroll. Validates inline, acks `starting`, then enrolls off
// the MQTT callback (detection + embedding training takes seconds).
func (h *DeviceMQTTHandler) handleFaceEnroll(env domain.MQTTDataCommand) error {
	req, errMsg := parseFaceEnrollData(env.Data)
	if errMsg != "" {
		slog.Warn("face.enroll: rejected", "component", "mqtt", "error", errMsg)
		return h.publishDataResult(domain.KindFaceEnroll, "failure", errMsg, nil)
	}

	// Label only: the image is biometric data and must not reach the journal.
	slog.Info("face.enroll: received", "component", "mqtt", "label", req.Label)
	if err := h.publishDataResult(domain.KindFaceEnroll, "starting", "", nil); err != nil {
		slog.Warn("face.enroll: publish starting ack failed", "component", "mqtt", "error", err)
	}

	go func() {
		faceEnrollMu.Lock()
		res, err := hal.FaceEnroll(req)
		faceEnrollMu.Unlock()
		if err != nil {
			slog.Error("face.enroll: failed", "component", "mqtt", "label", req.Label, "error", err)
			if pubErr := h.publishDataResult(domain.KindFaceEnroll, "failure", err.Error(), nil); pubErr != nil {
				slog.Warn("face.enroll: publish failure ack failed", "component", "mqtt", "error", pubErr)
			}
			return
		}
		slog.Info("face.enroll: success", "component", "mqtt", "label", res.Label, "enrolled_count", res.EnrolledCount)
		if pubErr := h.publishDataResult(domain.KindFaceEnroll, "success", "", res); pubErr != nil {
			slog.Warn("face.enroll: publish success ack failed", "component", "mqtt", "error", pubErr)
		}
	}()
	return nil
}

// parseFaceEnrollData validates a face.enroll payload; errMsg is "" on success.
func parseFaceEnrollData(raw json.RawMessage) (hal.FaceEnrollRequest, string) {
	var d domain.MQTTFaceEnrollData
	if err := json.Unmarshal(raw, &d); err != nil {
		return hal.FaceEnrollRequest{}, "invalid face.enroll data: " + err.Error()
	}
	label := strings.TrimSpace(d.Label)
	if label == "" {
		return hal.FaceEnrollRequest{}, "label is required"
	}
	if len([]rune(label)) > faceEnrollLabelMaxLen {
		return hal.FaceEnrollRequest{}, "label exceeds 64 characters"
	}
	encoded := strings.TrimSpace(d.ImageBase64)
	// Tolerate a browser data URL ("data:image/jpeg;base64,...").
	if strings.HasPrefix(encoded, "data:") {
		if i := strings.Index(encoded, ","); i >= 0 {
			encoded = encoded[i+1:]
		}
	}
	if encoded == "" {
		return hal.FaceEnrollRequest{}, "image_base64 is required"
	}
	if len(encoded) > base64.StdEncoding.EncodedLen(faceEnrollMaxBytes) {
		return hal.FaceEnrollRequest{}, "image exceeds 10 MiB"
	}
	img, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return hal.FaceEnrollRequest{}, "image_base64 must be valid base64"
	}
	if len(img) == 0 {
		return hal.FaceEnrollRequest{}, "image is empty"
	}
	if len(img) > faceEnrollMaxBytes {
		return hal.FaceEnrollRequest{}, "image exceeds 10 MiB"
	}
	return hal.FaceEnrollRequest{
		ImageBase64:      encoded,
		Label:            label,
		TelegramUsername: strings.TrimSpace(d.TelegramUsername),
		TelegramID:       strings.TrimSpace(d.TelegramID),
	}, ""
}
