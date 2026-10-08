package system

import (
	"slices"
	"testing"

	"go.autonomous.ai/os/system/lib/syspath"
)

// Unshipped logs can hold the previous owner's speech; left behind, they would
// ship with the next owner's key after the device is set up again.
func TestFactoryResetWipesTheLogSpool(t *testing.T) {
	if !slices.Contains(deviceWipePaths, syspath.GELFSpoolDir()) {
		t.Fatalf("deviceWipePaths %v does not include the GELF spool %s", deviceWipePaths, syspath.GELFSpoolDir())
	}
}

// A second owner must not inherit the first owner's transcripts, channel
// history or paired Mac.
func TestFactoryResetWipesThePreviousOwnersHistory(t *testing.T) {
	for _, p := range []string{
		"/root/config/buddies.json",
		"/root/config/config.json.corrupt",
		"/root/local/external-history",
	} {
		if !slices.Contains(deviceWipePaths, p) {
			t.Errorf("deviceWipePaths does not include %s", p)
		}
	}
	if !slices.Contains(deviceWipeGlobs, "/root/local/flow_events_*.jsonl") {
		t.Errorf("deviceWipeGlobs %v does not include the Flow Monitor turn logs", deviceWipeGlobs)
	}
}
