package mqtthandler

import (
	"context"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/alert"
)

// alertOps sends a best-effort ops alert about a DEVICE ACTION to
// bff-campaign-service (which owns the Telegram bot token + maintainer chat).
func (h *DeviceMQTTHandler) alertOps(title, detail string) {
	alert.Notifyf(context.Background(), h.config, title, detail)
}

// alertOAuthStateChange alerts the maintainer chat only when a provider's
// OAuth refresh outcome flips (ok<->fail), so a healthy refresh loop stays
// silent.
func (h *DeviceMQTTHandler) alertOAuthStateChange(provider, status, detail string) {
	if h.oauthAlertStatus == nil {
		h.oauthAlertStatus = map[string]string{}
	}
	prev := h.oauthAlertStatus[provider]
	if prev == status {
		return
	}
	h.oauthAlertStatus[provider] = status
	switch status {
	case "fail":
		h.alertOps("❌ OAuth refresh "+provider+" — FAILED", detail)
	case "ok":
		if prev == "fail" {
			h.alertOps("✅ OAuth refresh "+provider+" — recovered", "")
		}
	}
}

// alertPairingTerminal alerts on a terminal pairing event (success / failure /
// timeout) for a streaming pair/login flow, ignoring intermediate QR/URL frames.
func (h *DeviceMQTTHandler) alertPairingTerminal(flow string, evt domain.PairingEvent) {
	switch evt.Status {
	case domain.PairingStatusSuccess:
		h.alertOps("✅ "+flow+" — paired", "")
	case domain.PairingStatusFailure, domain.PairingStatusTimeout:
		h.alertOps("❌ "+flow+" — "+string(evt.Status), evt.Error)
	}
}
