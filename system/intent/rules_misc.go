package intent

import (
	"fmt"
	"time"
)

var miscRules = []rule{
	{
		name:  "what_time",
		match: anyOf("what time", "whats the time", "what's the time"),
		exec: func(string) *Result {
			now := time.Now()
			text := fmt.Sprintf("It's %s.", now.Format("3:04 PM"))
			return &Result{TTSText: text, Actions: []string{"time.Now()"}}
		},
	},
}
