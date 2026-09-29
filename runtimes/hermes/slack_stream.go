package hermes

import (
	"log/slog"
	"sync"
	"time"
)

// slackStreamFlushInterval throttles chat.appendStream (Tier 4, 100+/min).
const slackStreamFlushInterval = 650 * time.Millisecond

// slackStream is a live Slack streaming reply for one run, opened lazily on the first content chunk.
type slackStream struct {
	channel  string
	threadTS string
	teamID   string

	mu          sync.Mutex
	ts          string // streaming message ts — set once chat.startStream succeeds
	started     bool   // true once chat.startStream has opened the message
	latest      string // latest cleaned cumulative reply text
	appendedLen int    // bytes of `latest` already sent to Slack

	kick    chan struct{} // nudges an immediate (throttled) flush on new content
	stop    chan struct{} // closed by finishSlackStream to end the goroutine
	stopped chan struct{} // closed by the goroutine after its final flush + stopStream
}

// startSlackStreamSession registers a (not-yet-opened) stream for runID and starts its append loop. chat.startStream is called lazily on the first content.
func (s *HermesService) startSlackStreamSession(runID, channel, threadTS, teamID string) {
	st := &slackStream{
		channel:  channel,
		threadTS: threadTS,
		teamID:   teamID,
		kick:     make(chan struct{}, 1),
		stop:     make(chan struct{}),
		stopped:  make(chan struct{}),
	}
	s.slackStreamsMu.Lock()
	s.slackStreams[runID] = st
	s.slackStreamsMu.Unlock()
	go s.runSlackStream(st)
}

// runSlackStream flushes new text to Slack on each kick (throttled) / tick until stopped.
func (s *HermesService) runSlackStream(st *slackStream) {
	ticker := time.NewTicker(slackStreamFlushInterval)
	defer ticker.Stop()
	var lastFlush time.Time
	for {
		select {
		case <-st.kick:
			s.flushSlackStream(st, &lastFlush, false)
		case <-ticker.C:
			s.flushSlackStream(st, &lastFlush, false)
		case <-st.stop:
			s.flushSlackStream(st, &lastFlush, true)
			st.mu.Lock()
			started, ts := st.started, st.ts
			st.mu.Unlock()
			if started {
				if err := s.stopSlackStream(st.channel, ts); err != nil {
					slog.Debug("slack: stopStream failed (non-fatal)", "component", "hermes", "err", err)
				}
			}
			close(st.stopped)
			return
		}
	}
}

// flushSlackStream sends the un-sent tail of latest.
func (s *HermesService) flushSlackStream(st *slackStream, lastFlush *time.Time, force bool) {
	if !force && time.Since(*lastFlush) < slackStreamFlushInterval {
		return
	}
	st.mu.Lock()
	pending := ""
	if len(st.latest) > st.appendedLen {
		pending = st.latest[st.appendedLen:]
	}
	started := st.started
	st.mu.Unlock()
	if pending == "" {
		return
	}

	if !started {
		ts, err := s.startSlackStream(st.channel, st.threadTS, st.teamID, pending)
		if err != nil {
			slog.Debug("slack: startStream failed (non-fatal, will fall back)", "component", "hermes", "err", err)
			return
		}
		st.mu.Lock()
		st.ts, st.started = ts, true
		st.appendedLen += len(pending)
		st.mu.Unlock()
	} else {
		if err := s.appendSlackStream(st.channel, st.ts, pending); err != nil {
			slog.Debug("slack: appendStream failed (non-fatal)", "component", "hermes", "err", err)
			return
		}
		st.mu.Lock()
		st.appendedLen += len(pending)
		st.mu.Unlock()
	}
	*lastFlush = time.Now()
}

// StreamSlackDelta implements domain.SlackBridge — records the latest cleaned cumulative text for runID and nudges the append loop.
func (s *HermesService) StreamSlackDelta(runID, cleanTextSoFar string) {
	s.slackStreamsMu.Lock()
	st := s.slackStreams[runID]
	s.slackStreamsMu.Unlock()
	if st == nil {
		return
	}
	st.mu.Lock()
	if len(cleanTextSoFar) >= len(st.latest) {
		st.latest = cleanTextSoFar
	}
	st.mu.Unlock()
	select {
	case st.kick <- struct{}{}:
	default:
	}
}

// finishSlackStream finalizes the stream for runID: records the final text, signals the goroutine to flush + chat.stopStream, waits, and removes the session.
func (s *HermesService) finishSlackStream(runID, finalText string) bool {
	s.slackStreamsMu.Lock()
	st := s.slackStreams[runID]
	delete(s.slackStreams, runID)
	s.slackStreamsMu.Unlock()
	if st == nil {
		return false
	}
	if finalText != "" {
		st.mu.Lock()
		if len(finalText) >= len(st.latest) {
			st.latest = finalText
		}
		st.mu.Unlock()
	}
	close(st.stop)
	<-st.stopped
	st.mu.Lock()
	started := st.started
	st.mu.Unlock()
	return started
}
