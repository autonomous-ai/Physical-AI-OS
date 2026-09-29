package urlnorm

import "strings"

// NormalizeBaseURL appends the /v1 OpenAI-compat prefix to Autonomous base URLs; others are unchanged.
func NormalizeBaseURL(base string) string {
	base = strings.TrimSuffix(strings.TrimSpace(base), "/")
	if strings.Contains(base, "campaign-api.autonomous.ai") && strings.HasSuffix(base, "/ai") {
		base += "/v1"
	}
	return base
}
