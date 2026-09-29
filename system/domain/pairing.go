package domain

import "time"

// PairingEventStatus is a lifecycle state of a streaming pairing/login flow (sent as the MQTT Status).
type PairingEventStatus string

const (
	PairingStatusStarting PairingEventStatus = "pairing_starting"
	PairingStatusQR       PairingEventStatus = "pairing_qr"
	// PairingStatusURL carries an OAuth URL in PairingEvent.URL; the flow then awaits SubmitClaudeLoginCode.
	PairingStatusURL PairingEventStatus = "pairing_url"
	// PairingStatusSuccess is the single "ready" terminal status (new or resumed session).
	PairingStatusSuccess PairingEventStatus = "success"
	PairingStatusTimeout PairingEventStatus = "timeout"
	PairingStatusFailure PairingEventStatus = "failure"
)

// PairingEvent is one update from a streaming pairing/login flow; QR fields and URL are set per Status.
type PairingEvent struct {
	Status    PairingEventStatus `json:"status"`
	QRText    string             `json:"qr_text,omitempty"`
	QRSeq     int                `json:"qr_seq,omitempty"`
	URL       string             `json:"url,omitempty"`
	ExpiresAt time.Time          `json:"expires_at,omitempty"`
	Error     string             `json:"error,omitempty"`
}
