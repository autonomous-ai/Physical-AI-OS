package buddy

import "claude-desktop-buddy/httpapi"

// StatusReader adapts StateMachine to httpapi.StatusProvider.
type StatusReader struct{ sm *StateMachine }

func NewStatusReader(sm *StateMachine) StatusReader { return StatusReader{sm: sm} }

func (s StatusReader) Status() httpapi.Status {
	st := httpapi.Status{
		State:     string(s.sm.State()),
		Connected: s.sm.Connected(),
	}
	if hb := s.sm.LastHeartbeat(); hb != nil {
		st.SessionsRunning = hb.Running
		st.TokensToday = hb.TokensToday
	}
	if p := s.sm.PendingPrompt(); p != nil {
		st.HasPending = true
		st.PendingID = p.ID
		st.PendingTool = p.Tool
		st.PendingHint = p.Hint
	}
	return st
}
