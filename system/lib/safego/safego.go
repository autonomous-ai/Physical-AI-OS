package safego

import (
	"log/slog"
	"runtime/debug"
)

// Go launches fn in a goroutine that logs a panic instead of crashing the process.
func Go(name string, fn func()) {
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("goroutine panic recovered",
					"component", name,
					"panic", r,
					"stack", string(debug.Stack()),
				)
			}
		}()
		fn()
	}()
}
