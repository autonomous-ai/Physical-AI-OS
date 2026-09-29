package hermes

import (
	"context"
	"errors"
	"flag"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestHardwareStartupRejectedByLegacyFlags(t *testing.T) {
	const prefix = "[Service]\nExecStartPre=-/usr/local/bin/os-server "
	if !strings.HasPrefix(gatewayHardwareStartupConfig, prefix) {
		t.Fatal("hardware wait must remain an optional pre-start command")
	}
	args := strings.Fields(strings.TrimPrefix(gatewayHardwareStartupConfig, prefix))
	legacy := flag.NewFlagSet("legacy-os-server", flag.ContinueOnError)
	legacy.SetOutput(io.Discard)
	legacy.Bool("version", false, "print version")
	if err := legacy.Parse(args); err == nil {
		t.Fatal("legacy OS would initialize services instead of rejecting the helper")
	}
	installer, err := os.ReadFile("install.sh")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(installer), "ExecStartPre=-/usr/local/bin/os-server --wait-hal-ready") {
		t.Fatal("installer must use the same backwards-compatible flag")
	}
}

func TestGatewayRestartAllowsDrainAndHardwareWait(t *testing.T) {
	var commandCtx context.Context
	failure := errors.New("systemctl failure")
	err := restartHermesGatewayWithRunner(func(ctx context.Context) ([]byte, error) {
		commandCtx = ctx
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) < 130*time.Second || time.Until(deadline) > 3*time.Minute {
			t.Fatal("restart must allow drain + HAL wait while remaining bounded")
		}
		return []byte("gateway failed"), failure
	})
	if !errors.Is(err, failure) || !strings.Contains(err.Error(), "gateway failed") {
		t.Fatalf("lost command failure: %v", err)
	}
	if commandCtx.Err() != context.Canceled {
		t.Fatal("restart command context not released")
	}
}

func TestGatewayHardwareStartupIdempotentAndPreservesUpstream(t *testing.T) {
	dir := t.TempDir()
	unit := filepath.Join(dir, "hermes-gateway.service")
	original := "[Service]\nExecStartPre=/existing/check\nExecStart=/original/hermes\n"
	if err := os.WriteFile(unit, []byte(original), 0644); err != nil {
		t.Fatal(err)
	}
	dropin := filepath.Join(unit+".d", "20-hardware-startup.conf")
	if err := os.MkdirAll(filepath.Dir(dropin), 0755); err != nil {
		t.Fatal(err)
	}
	other := filepath.Join(filepath.Dir(dropin), "10-custom.conf")
	if err := os.WriteFile(other, []byte("[Service]\nEnvironment=KEEP=1\n"), 0644); err != nil {
		t.Fatal(err)
	}
	reloads := 0
	reload := func() error { reloads++; return nil }
	for i := 0; i < 2; i++ {
		if err := writeGatewayHardwareStartup(dropin, reload); err != nil {
			t.Fatal(err)
		}
	}
	if reloads != 1 {
		t.Fatalf("reloads=%d, want 1", reloads)
	}
	got, err := os.ReadFile(dropin)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != gatewayHardwareStartupConfig {
		t.Fatalf("unexpected drop-in %q", got)
	}
	info, err := os.Stat(dropin)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0644 {
		t.Fatalf("mode %o", info.Mode().Perm())
	}
	got, err = os.ReadFile(unit)
	if err != nil || string(got) != original {
		t.Fatalf("upstream changed: %q, %v", got, err)
	}
	got, err = os.ReadFile(other)
	if err != nil || !strings.Contains(string(got), "KEEP=1") {
		t.Fatal("other drop-in changed")
	}
}

func TestGatewayHardwareStartupReloadFailureRestoresForRetry(t *testing.T) {
	for _, existing := range []bool{false, true} {
		name := "absent"
		if existing {
			name = "existing"
		}
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "20-hardware-startup.conf")
			previous := []byte("[Service]\n# previous managed file\n")
			if existing {
				if err := os.WriteFile(path, previous, 0644); err != nil {
					t.Fatal(err)
				}
			}
			failure := errors.New("reload failed")
			if err := writeGatewayHardwareStartup(path, func() error { return failure }); !errors.Is(err, failure) {
				t.Fatalf("error=%v", err)
			}
			got, err := os.ReadFile(path)
			if existing {
				if err != nil || string(got) != string(previous) {
					t.Fatal("previous content not restored")
				}
			} else if !os.IsNotExist(err) {
				t.Fatal("new drop-in not removed")
			}
			reloads := 0
			if err := writeGatewayHardwareStartup(path, func() error { reloads++; return nil }); err != nil {
				t.Fatal(err)
			}
			if reloads != 1 {
				t.Fatal("failed reload not retried")
			}
		})
	}
}

func TestGatewayHardwareStartupWriteFailureDoesNotReload(t *testing.T) {
	path := filepath.Join(t.TempDir(), "not-a-directory")
	if err := os.WriteFile(path, []byte("keep"), 0644); err != nil {
		t.Fatal(err)
	}
	err := writeGatewayHardwareStartup(filepath.Join(path, "20-hardware-startup.conf"), func() error { t.Error("reload after failed write"); return nil })
	if err == nil {
		t.Fatal("expected write failure")
	}
}
