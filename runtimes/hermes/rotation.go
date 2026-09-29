package hermes

import (
	"fmt"
	"log/slog"
	"time"
)

// rotateMaxTurns / rotateTokenThreshold gate conversation rotation (see ShouldRotateSession).
// 250k sits above where gateway-side compression settles (~12-75k); 50k dropped history every 2-3 turns.
const (
	rotateMaxTurns       = 40
	rotateTokenThreshold = 250_000
)

// initConversation seeds a boot-unique conversation name so a restart never re-attaches to a bloated chain.
func (s *HermesService) initConversation() {
	s.convOnce.Do(func() {
		s.bootStamp = time.Now().Unix()
		s.conversation.Store(fmt.Sprintf("%s-%d", Conversation, s.bootStamp))
	})
}

// conversationName returns the active conversation name sent on every turn.
func (s *HermesService) conversationName() string {
	s.initConversation()
	name, _ := s.conversation.Load().(string)
	return name
}

// rotateConversation switches future turns to a fresh conversation name so the gateway starts a new (small) history chain.
func (s *HermesService) rotateConversation() {
	s.initConversation()
	seq := s.rotateSeq.Add(1)
	name := fmt.Sprintf("%s-%d-%d", Conversation, s.bootStamp, seq)
	s.conversation.Store(name)
	s.lastResponseID.Store("")
	s.sessionUUID.Store("")
	s.steeringMu.Lock()
	s.managedSession = ""
	s.managedSessionSet = true
	s.steeringMu.Unlock()
	slog.Info("hermes conversation rotated", "component", "hermes", "conversation", name)
}

// ShouldRotateSession overrides the generic handler's token-threshold rotation decision (the sessionRotator optional interface).
func (s *HermesService) ShouldRotateSession(totalTokens, turnsSinceRotation int) bool {
	return turnsSinceRotation >= rotateMaxTurns || totalTokens >= rotateTokenThreshold
}

// NewSession rotates the conversation.
func (s *HermesService) NewSession(sessionKey string) error {
	slog.Info("hermes NewSession: rotating conversation", "component", "hermes", "key", sessionKey)
	s.rotateConversation()
	return nil
}
