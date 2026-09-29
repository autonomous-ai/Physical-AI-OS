package domain

// SlackInbound carries a Slack Events API delivery forwarded verbatim over MQTT.
type SlackInbound struct {
	Body string
}

// SlackBridge is an optional gateway interface for runtimes whose Slack frontend lives in os-server (hermes).
type SlackBridge interface {
	// HandleInboundSlack parses a forwarded Slack event and drives an agent turn.
	// challenge != "" must be echoed back; handled == false means ignored (ack, no turn).
	HandleInboundSlack(in SlackInbound) (challenge string, handled bool, err error)

	// IsSlackOriginRun reports whether runID came from Slack, without consuming the origin.
	IsSlackOriginRun(runID string) bool

	// StreamSlackDelta feeds the full sanitized reply text so far; the bridge appends only the new portion.
	StreamSlackDelta(runID string, cleanTextSoFar string)

	// DeliverSlackReply finalizes a Slack-origin turn and consumes the origin; no-op for non-Slack runs.
	DeliverSlackReply(runID string, text string) error
}
