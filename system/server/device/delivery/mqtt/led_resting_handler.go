package mqtthandler

import (
	"encoding/json"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
)

// The led.resting.* kinds are the MQTT twin of the web "Resting light"
// settings: same HAL calls (/led/resting), same validation. HAL owns the look,
// saves the choice and repaints the strip; os-server only relays.

// errNoLight is returned on devices that declare no light capability.
const errNoLight = "this device has no light"

// validRestingColor checks an [r, g, b] with channels 0-255; "" on success.
func validRestingColor(c []int) string {
	if len(c) != 3 {
		return "color must be [r, g, b]"
	}
	for _, v := range c {
		if v < 0 || v > 255 {
			return "color channels must be 0-255"
		}
	}
	return ""
}

func (h *DeviceMQTTHandler) hasLight() bool {
	return device.Has(h.config.DeviceTypeOrDefault(), device.CapLight)
}

// handleLEDRestingGet handles kind="led.resting.get": the owner's choice, the
// device default and the look in effect. Synchronous.
func (h *DeviceMQTTHandler) handleLEDRestingGet(env domain.MQTTDataCommand) error {
	if !h.hasLight() {
		return h.publishDataResult(domain.KindLEDRestingGet, "failure", errNoLight, nil)
	}
	res, err := hal.GetRestingLED()
	if err != nil {
		slog.Error("led.resting.get: failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindLEDRestingGet, "failure", err.Error(), nil)
	}
	return h.publishDataResult(domain.KindLEDRestingGet, "success", "", res)
}

// handleLEDRestingSet handles kind="led.resting.set" — the app's Save: HAL
// stores the choice (it survives reboots) and shows it now. Synchronous.
func (h *DeviceMQTTHandler) handleLEDRestingSet(env domain.MQTTDataCommand) error {
	var d domain.MQTTLEDRestingData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindLEDRestingSet, "failure", "invalid led.resting.set data: "+err.Error(), nil)
	}
	if !h.hasLight() {
		return h.publishDataResult(domain.KindLEDRestingSet, "failure", errNoLight, nil)
	}
	choice := hal.RestingLEDChoice{Mode: d.Mode}
	switch d.Mode {
	case "default", "off":
	case "custom":
		if msg := validRestingColor(d.Color); msg != "" {
			return h.publishDataResult(domain.KindLEDRestingSet, "failure", msg, nil)
		}
		choice.Color = d.Color
	default:
		return h.publishDataResult(domain.KindLEDRestingSet, "failure",
			fmt.Sprintf("mode must be default, off or custom (got %q)", d.Mode), nil)
	}
	res, err := hal.SetRestingLED(choice)
	if err != nil {
		slog.Error("led.resting.set: failed", "component", "mqtt", "mode", d.Mode, "error", err)
		return h.publishDataResult(domain.KindLEDRestingSet, "failure", err.Error(), nil)
	}
	slog.Info("led.resting.set: saved", "component", "mqtt", "mode", res.Mode, "color", res.Color)
	return h.publishDataResult(domain.KindLEDRestingSet, "success", "", res)
}

// handleLEDRestingPreview handles kind="led.resting.preview" — sent while the
// owner drags a colour: HAL paints it without saving and falls back to the
// saved look 10 s after the last preview. Only failures are acked, so a drag
// does not flood fd_channel with replies.
func (h *DeviceMQTTHandler) handleLEDRestingPreview(env domain.MQTTDataCommand) error {
	var d domain.MQTTLEDRestingData
	if err := json.Unmarshal(env.Data, &d); err != nil {
		return h.publishDataResult(domain.KindLEDRestingPreview, "failure", "invalid led.resting.preview data: "+err.Error(), nil)
	}
	if !h.hasLight() {
		return h.publishDataResult(domain.KindLEDRestingPreview, "failure", errNoLight, nil)
	}
	if msg := validRestingColor(d.Color); msg != "" {
		return h.publishDataResult(domain.KindLEDRestingPreview, "failure", msg, nil)
	}
	if _, err := hal.PreviewRestingLED(d.Color); err != nil {
		slog.Warn("led.resting.preview: failed", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindLEDRestingPreview, "failure", err.Error(), nil)
	}
	return nil
}
