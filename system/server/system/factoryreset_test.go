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
