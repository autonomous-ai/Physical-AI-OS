package reconnect

import (
	"testing"
	"time"
)

func TestOnlyALongOutageIsAnnounced(t *testing.T) {
	base := time.Unix(1_700_000_000, 0)
	clock := base
	now = func() time.Time { return clock }
	defer func() { now = time.Now }()

	var n Notice
	if n.Up() {
		t.Fatal("a first connection is never a reconnect")
	}
	n.Down()
	clock = base.Add(5 * time.Second)
	if n.Up() {
		t.Fatal("a 5 s blip (an MCP write, a token refresh) must stay silent")
	}
	n.Down()
	clock = base.Add(5*time.Second + MinOutage)
	if !n.Up() {
		t.Fatal("an outage of MinOutage must be announced")
	}
	if n.Up() {
		t.Fatal("Up resets the clock; a second Up announces nothing")
	}
}

func TestRepeatedDownKeepsTheEarliestTime(t *testing.T) {
	base := time.Unix(1_700_000_000, 0)
	clock := base
	now = func() time.Time { return clock }
	defer func() { now = time.Now }()

	var n Notice
	n.Down()
	clock = base.Add(15 * time.Second)
	n.Down() // a retry loop calling Down again must not restart the outage
	clock = base.Add(25 * time.Second)
	if !n.Up() {
		t.Fatal("the outage started at the first Down")
	}
}
