package server

// HarnessTaskPending reports registered response ownership even after the short
// follow-up window expires. It neither consults nor mutates Store preparations.
func (s *Server) HarnessTaskPending() bool {
	s.harnessRepliesMu.Lock()
	defer s.harnessRepliesMu.Unlock()
	return len(s.harnessReplies) > 0
}
