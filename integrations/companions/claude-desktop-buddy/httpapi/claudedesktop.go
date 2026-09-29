package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
)

// ApprovalRequest is the body of POST /claude-desktop/approve and /deny.
type ApprovalRequest struct {
	ID string `json:"id"`
}

func (s *Server) handleApprove(w http.ResponseWriter, r *http.Request) {
	s.decide(w, r, s.approvals.Approve)
}

func (s *Server) handleDeny(w http.ResponseWriter, r *http.Request) {
	s.decide(w, r, s.approvals.Deny)
}

// decide runs the shared approve/deny flow and maps the outcome to an HTTP status.
func (s *Server) decide(w http.ResponseWriter, r *http.Request, action func(id string) error) {
	var req ApprovalRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		fail(w, http.StatusBadRequest, "invalid json")
		return
	}
	switch err := action(req.ID); {
	case errors.Is(err, ErrNoPending), errors.Is(err, ErrPromptMismatch):
		fail(w, http.StatusConflict, err.Error())
	case err != nil:
		fail(w, http.StatusInternalServerError, "ble send failed")
	default:
		ok(w)
	}
}
