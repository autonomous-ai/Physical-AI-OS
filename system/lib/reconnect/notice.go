// Package reconnect decides whether a brain reconnect is worth saying out loud.
package reconnect

import (
	"sync/atomic"
	"time"
)

// MinOutage is how long the brain must have been gone before the device
// announces it is back. Planned restarts (an MCP entry written, a token
// refreshed, a runtime switch) reconnect within seconds and stay silent; only
// an outage the user could have noticed earns "Oh, I can think again!".
const MinOutage = 20 * time.Second

// now is replaceable for tests.
var now = time.Now

// Notice tracks one connection's outage. The zero value is ready to use.
type Notice struct {
	downSince atomic.Int64 // unix nanos of the first Down since the last Up; 0 = up
}

// Down records the connection going away; repeated calls keep the earliest time.
func (n *Notice) Down() {
	n.downSince.CompareAndSwap(0, now().UnixNano())
}

// Up records the connection returning and reports whether the outage was long
// enough to announce. Always call it on reconnect so the clock resets.
func (n *Notice) Up() bool {
	since := n.downSince.Swap(0)
	if since == 0 {
		return false
	}
	return now().Sub(time.Unix(0, since)) >= MinOutage
}
