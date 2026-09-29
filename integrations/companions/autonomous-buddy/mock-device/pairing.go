package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

type pairStartResponse struct {
	Code      string `json:"code"`
	ExpiresIn int    `json:"expires_in"`
}

type pairConfirmRequest struct {
	Code        string `json:"code"`
	Name        string `json:"name"`
	Fingerprint string `json:"fingerprint"`
	OSVersion   string `json:"os_version"`
}

type pairConfirmResponse struct {
	Token   string `json:"token"`
	BuddyID string `json:"buddy_id"`
}

// HandlePairStart issues a fresh 6-digit code (no admin auth in the mock).
func (s *State) HandlePairStart(w http.ResponseWriter, r *http.Request) {
	code := s.IssueCode()
	writeJSON(w, http.StatusOK, pairStartResponse{Code: code, ExpiresIn: 300})
}

// HandlePairConfirm validates the submitted code and issues a long-lived bearer token.
func (s *State) HandlePairConfirm(w http.ResponseWriter, r *http.Request) {
	var req pairConfirmRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	if !s.consumeCode(req.Code) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid or expired code"})
		return
	}
	record := PairingRecord{
		Token:       newToken(),
		BuddyID:     newBuddyID(),
		Name:        req.Name,
		Fingerprint: req.Fingerprint,
		OSVersion:   req.OSVersion,
	}
	s.savePairing(record)
	logf("✓ buddy paired: name=%q os=%q id=%s", req.Name, req.OSVersion, record.BuddyID)
	writeJSON(w, http.StatusOK, pairConfirmResponse{Token: record.Token, BuddyID: record.BuddyID})
}

// HandleSelfRevoke drops the pairing when the buddy app unpairs itself with its own Bearer token.
func (s *State) HandleSelfRevoke(w http.ResponseWriter, r *http.Request) {
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "missing bearer"})
		return
	}
	token := strings.TrimPrefix(auth, "Bearer ")
	record := s.lookupByToken(token)
	if record == nil {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid token"})
		return
	}
	s.clearPairing()
	logf("✓ buddy self-revoked: %s", record.BuddyID)
	writeJSON(w, http.StatusOK, map[string]bool{"revoked": true})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func logf(format string, args ...any) {
	fmt.Printf("[mock-device] "+format+"\n", args...)
}
