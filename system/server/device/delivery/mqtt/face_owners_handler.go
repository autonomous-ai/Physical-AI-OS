package mqtthandler

import (
	"encoding/json"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
)

// faceSharedBucket is HAL's log bucket for unidentified people; never an enrolled person.
const faceSharedBucket = "unknown"

// handleFaceOwners handles kind="face.owners" — lists enrolled people via
// HAL GET /face/owners. Synchronous: the listing is a directory scan.
func (h *DeviceMQTTHandler) handleFaceOwners(env domain.MQTTDataCommand) error {
	owners, err := hal.GetFaceOwners()
	if err != nil {
		slog.Error("face.owners: failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindFaceOwners, "failure", err.Error(), nil)
	}
	return h.publishDataResult(domain.KindFaceOwners, "success", "", enrolledOnly(owners))
}

// enrolledOnly drops HAL's shared "unknown" bucket so the list holds real people only.
func enrolledOnly(o *hal.FaceOwners) *hal.FaceOwners {
	persons := make([]hal.FaceOwner, 0, len(o.Persons))
	for _, p := range o.Persons {
		if p.Label == faceSharedBucket {
			continue
		}
		if p.Photos == nil {
			p.Photos = []string{}
		}
		persons = append(persons, p)
	}
	return &hal.FaceOwners{EnrolledCount: o.EnrolledCount, Persons: persons}
}

// handleFaceRemove handles kind="face.remove" — deletes one person via HAL
// POST /face/remove. Acks `starting`, then runs off the MQTT callback since
// HAL retrains from the remaining photos.
func (h *DeviceMQTTHandler) handleFaceRemove(env domain.MQTTDataCommand) error {
	var d domain.MQTTFaceRemoveData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindFaceRemove, "failure", "invalid face.remove data: "+err.Error(), nil)
	}
	label := strings.TrimSpace(d.Label)
	if label == "" {
		return h.publishDataResult(domain.KindFaceRemove, "failure", "label is required", nil)
	}
	if len([]rune(label)) > faceEnrollLabelMaxLen {
		return h.publishDataResult(domain.KindFaceRemove, "failure", "label exceeds 64 characters", nil)
	}
	if strings.EqualFold(label, faceSharedBucket) {
		return h.publishDataResult(domain.KindFaceRemove, "failure", "\"unknown\" is not an enrolled person", nil)
	}

	slog.Info("face.remove: received", "component", "mqtt", "label", label)
	if err := h.publishDataResult(domain.KindFaceRemove, "starting", "", nil); err != nil {
		slog.Warn("face.remove: publish starting ack failed", "component", "mqtt", "error", err)
	}

	go func() {
		faceEnrollMu.Lock()
		res, err := hal.FaceRemove(label)
		faceEnrollMu.Unlock()
		if err != nil {
			slog.Error("face.remove: failed", "component", "mqtt", "label", label, "error", err)
			if pubErr := h.publishDataResult(domain.KindFaceRemove, "failure", err.Error(), nil); pubErr != nil {
				slog.Warn("face.remove: publish failure ack failed", "component", "mqtt", "error", pubErr)
			}
			return
		}
		slog.Info("face.remove: success", "component", "mqtt", "label", res.Label, "enrolled_count", res.EnrolledCount)
		if pubErr := h.publishDataResult(domain.KindFaceRemove, "success", "", res); pubErr != nil {
			slog.Warn("face.remove: publish success ack failed", "component", "mqtt", "error", pubErr)
		}
	}()
	return nil
}
