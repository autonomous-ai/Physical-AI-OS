package domain

import (
	"encoding/json"
	"strings"
)

// WSEvent represents a gateway WebSocket event frame.
type WSEvent struct {
	Type    string          `json:"type"`
	Event   string          `json:"event"`
	Payload json.RawMessage `json:"payload"`
}

// AgentPayload is an agent event; Stream is "lifecycle", "tool", "assistant" or "thinking".
type AgentPayload struct {
	RunID      string `json:"runId"`
	Stream     string `json:"stream"`
	SessionKey string `json:"sessionKey"`
	Seq        int    `json:"seq"`
	Ts         int64  `json:"ts"`
	Data       struct {
		MergedIntoActiveTurn bool   `json:"mergedIntoActiveTurn,omitempty"`
		Phase                string `json:"phase"`
		StartedAt            int64  `json:"startedAt,omitempty"`
		EndedAt              int64  `json:"endedAt,omitempty"`
		Error                string `json:"error,omitempty"`
		Aborted              bool   `json:"aborted,omitempty"`
		// "tool" is a legacy device alias of "name".
		Tool       string          `json:"tool,omitempty"`
		Name       string          `json:"name,omitempty"`
		ToolCallID string          `json:"toolCallId,omitempty"`
		ToolArgs   string          `json:"toolArgs,omitempty"`
		Args       json.RawMessage `json:"args,omitempty"`
		Arguments  string          `json:"arguments,omitempty"`
		// Result may be a string or structured object; keep RawMessage (string type broke the WS read loop).
		Result        json.RawMessage `json:"result,omitempty"`
		PartialResult json.RawMessage `json:"partialResult,omitempty"`
		Text          string          `json:"text,omitempty"`
		Delta         string          `json:"delta,omitempty"`
		Usage         *TokenUsage     `json:"usage,omitempty"`
	} `json:"data"`
}

// ToolName returns the resolved tool name from either "name" (OpenClaw) or "tool" (device legacy).
func (p *AgentPayload) ToolName() string {
	if p.Data.Name != "" {
		return p.Data.Name
	}
	return p.Data.Tool
}

// ToolArguments returns tool arguments as a string from whichever field is set.
func (p *AgentPayload) ToolArguments() string {
	if p.Data.ToolArgs != "" {
		return p.Data.ToolArgs
	}
	if p.Data.Arguments != "" {
		return p.Data.Arguments
	}
	if len(p.Data.Args) > 0 {
		return string(p.Data.Args)
	}
	return ""
}

// ResultText extracts readable text from Data.Result; falls back to raw JSON.
func (p *AgentPayload) ResultText() string { return resultRawToString(p.Data.Result) }

// PartialResultText is ResultText for Data.PartialResult.
func (p *AgentPayload) PartialResultText() string { return resultRawToString(p.Data.PartialResult) }

func resultRawToString(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var withText struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &withText) == nil && withText.Text != "" {
		return withText.Text
	}
	var withContent struct {
		Content []struct {
			Text string `json:"text"`
		} `json:"content"`
	}
	if json.Unmarshal(raw, &withContent) == nil && len(withContent.Content) > 0 {
		parts := make([]string, 0, len(withContent.Content))
		for _, c := range withContent.Content {
			if c.Text != "" {
				parts = append(parts, c.Text)
			}
		}
		if len(parts) > 0 {
			return strings.Join(parts, " ")
		}
	}
	return string(raw)
}

// TokenUsage captures LLM token consumption from an agent turn.
type TokenUsage struct {
	InputTokens       int `json:"inputTokens,omitempty"`
	OutputTokens      int `json:"outputTokens,omitempty"`
	CacheReadTokens   int `json:"cacheReadTokens,omitempty"`
	CacheWriteTokens  int `json:"cacheWriteTokens,omitempty"`
	TotalTokens       int `json:"totalTokens,omitempty"`
	CompressAtTokens  int `json:"compressAtTokens,omitempty"`
	SummarizeAtTokens int `json:"summarizeAtTokens,omitempty"`
}

// ChatPayload represents a chat stream event from the gateway.
type ChatPayload struct {
	RunID        string          `json:"runId"`
	SessionKey   string          `json:"sessionKey"`
	State        string          `json:"state"` // "partial", "final", "error"
	RawMessage   json.RawMessage `json:"message"`
	Message      string          `json:"-"`    // resolved from RawMessage
	Role         string          `json:"role"` // "assistant", "user"
	ErrorMessage string          `json:"errorMessage,omitempty"`
}

// ResolveChatMessage extracts the text from Message which can be a string or an object with a "text" field.
func (p *ChatPayload) ResolveChatMessage() {
	if len(p.RawMessage) == 0 {
		return
	}
	var s string
	if json.Unmarshal(p.RawMessage, &s) == nil {
		p.Message = s
		return
	}
	var obj struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(p.RawMessage, &obj) == nil {
		p.Message = obj.Text
		if p.Message != "" {
			return
		}
	}

	var generic map[string]any
	if json.Unmarshal(p.RawMessage, &generic) == nil {
		parts := make([]string, 0, 2)
		if v, ok := generic["content"]; ok {
			switch c := v.(type) {
			case string:
				if strings.TrimSpace(c) != "" {
					p.Message = c
					return
				}
			case []any:
				for _, item := range c {
					m, ok := item.(map[string]any)
					if !ok {
						continue
					}
					if t, ok := m["text"].(string); ok && strings.TrimSpace(t) != "" {
						parts = append(parts, t)
					}
				}
			}
		}
		if len(parts) > 0 {
			p.Message = strings.Join(parts, " ")
		}
	}
}
