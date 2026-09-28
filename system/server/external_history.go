package server

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"unicode/utf8"

	"go.autonomous.ai/os/system/externalhistory"
	"go.autonomous.ai/os/system/harness"
)

func (s *Server) initializeExternalHistory(ctx context.Context) error {
	store, err := externalhistory.New("local/external-history")
	if err != nil {
		return fmt.Errorf("initialize external conversation history: %w", err)
	}
	s.externalHistory = store
	s.sensingHandler.SetRealtimeHistory(store.RecordRealtime)
	store.RestoreSilent(s.agentGateway)
	s.agentHandler.SetExternalHistoryObserver(func(runID string, failed bool) {
		r, ok := store.Lookup(runID)
		if !ok || (r.State != externalhistory.StateSending && r.State != externalhistory.StateUncertain) {
			return
		}
		var err error
		if failed {
			err = store.MarkUncertain(runID)
		} else {
			err = store.Acknowledge(runID)
		}
		if err != nil {
			slog.Warn("external history acknowledgement failed", "run_id", runID, "error", err)
		}
	})
	go store.Run(ctx, s.agentGateway)
	return nil
}

// Only direct voice bypasses the main runtime. Delegated skill turns already
// contain their input there and keep their existing follow-up context behavior.
func (s *Server) beginHarnessHistory(runID, input string, state harness.VoiceModeState) error {
	if s.externalHistory == nil {
		return nil
	}
	_, err := s.externalHistory.Begin(externalhistory.Record{
		Source: "harness", OriginRunID: runID, MachineID: state.MachineID,
		AgentID: state.AgentID, AgentName: state.AgentName, Input: input,
	})
	return err
}

func (s *Server) completeHarnessHistory(runID, text string) error {
	if s.externalHistory == nil {
		return nil
	}
	for _, r := range s.externalHistory.Records() {
		if r.Source != "harness" || r.OriginRunID != runID {
			continue
		}
		// Like realtime's REPLY_SYNC limit, bound context without a summarizer.
		const suffix = " …[truncated]"
		if len(text) > externalhistory.MaxOutputBytes {
			text = text[:externalhistory.MaxOutputBytes-len(suffix)]
			for !utf8.ValidString(text) {
				text = text[:len(text)-1]
			}
			text += suffix
		}
		return s.externalHistory.Complete("harness", runID, strings.TrimSpace(text))
	}
	return nil
}

// Restore only unfinished voice response routes belonging to the same pairing.
// No request is resent and no latest recap is guessed for an unfinished run.
func (s *Server) restoreHarnessHistoryReplies() {
	if s.externalHistory == nil || s.harnessService == nil {
		return
	}
	reserved := map[string]harness.ResultInput{}
	answers := map[string]bool{}
	if s.harnessResults != nil {
		for _, in := range s.harnessResults.Inputs() {
			reserved[in.RunID] = in
		}
	}
	if s.harnessResults != nil {
		for _, answer := range s.harnessResults.Answers() {
			reserved[answer.Input.RunID] = answer.Input
			answers[answer.Input.RunID] = true
		}
	}
	status := s.harnessService.Status()
	for _, r := range s.externalHistory.Records() {
		if r.Source == "harness" && r.State == externalhistory.StateWaiting &&
			r.AgentID != "" && status.Paired && r.MachineID == status.MachineID {
			if in, exists := reserved[r.OriginRunID]; exists {
				if in.Owner == s.harnessService.ResultOwner() {
					s.restoreHarnessResultRoute(in)
					if answers[in.RunID] {
						s.harnessRepliesMu.Lock()
						route := s.harnessReplies[in.RunID]
						route.answer = true
						s.harnessReplies[in.RunID] = route
						s.harnessRepliesMu.Unlock()
					}
				}
				continue
			}
			s.registerHarnessReply(r.AgentID, r.OriginRunID, false, false)
		}
	}
}
