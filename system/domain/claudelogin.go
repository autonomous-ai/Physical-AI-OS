package domain

import "context"

// ClaudeLoginPairer is the optional gateway interface for the claude.ai OAuth login (claudecode only).
// Flow: pairing_starting -> pairing_url -> success | timeout | failure; callers must drain events.
type ClaudeLoginPairer interface {
	// StartClaudeLogin launches the login flow; at most one may be active (else "login_already_in_progress").
	StartClaudeLogin(ctx context.Context) <-chan PairingEvent

	// SubmitClaudeLoginCode feeds the browser authorization code into the waiting flow.
	SubmitClaudeLoginCode(code string) error
}
