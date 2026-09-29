package domain

// ChannelStartEmotioner is implemented by runtimes whose gateway owns channel I/O
// (hermes, picoclaw) to fire the "thinking" ack for native channel turns.
type ChannelStartEmotioner interface {
	// FireChannelStartEmotion fires the "thinking" ack; message is the inbound user text.
	FireChannelStartEmotion(message, runID string)
}
