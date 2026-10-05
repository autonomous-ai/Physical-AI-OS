package bootstrap

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/bootstrap/config"
	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/domain"
)

func TestPendingRecoveryFailureStopsEveryUpdateEntry(t *testing.T) {
	entries := map[string]func(*Bootstrap) error{
		"poll":      func(b *Bootstrap) error { return b.checkOnce(context.Background()) },
		"component": func(b *Bootstrap) error { return b.checkComponent(context.Background(), domain.OTAKeyHal) },
		"force":     func(b *Bootstrap) error { return b.forceUpdate(context.Background(), domain.OTAKeyHal) },
	}
	for name, run := range entries {
		t.Run(name, func(t *testing.T) {
			dir, calls := setupUpdateAudit(t)
			t.Setenv("UPDATE_TEST_FAIL", "recover")
			pending := filepath.Join(dir, "pending-update.json")
			if err := os.WriteFile(pending, []byte(`{"component":"hal"}`), 0600); err != nil {
				t.Fatal(err)
			}
			b := &Bootstrap{cfg: &config.Config{MetadataURL: "http://127.0.0.1:1/should-not-fetch"}, state: &state.State{Components: map[string]string{domain.OTAKeyHal: "9.0.0"}}, pendingUpdatePath: pending}
			if err := run(b); err == nil {
				t.Fatal("failed recovery must stop update entry")
			}
			data, err := os.ReadFile(calls)
			if err != nil || string(data) != "recover\n" {
				t.Fatalf("calls = %q, %v", data, err)
			}
			if b.state.Components[domain.OTAKeyHal] != "9.0.0" {
				t.Fatal("failed recovery changed recorded version")
			}
		})
	}
}

func TestPendingRecoveryRunsBeforeMetadataEvenWithCurrentStoredVersion(t *testing.T) {
	dir, calls := setupUpdateAudit(t)
	t.Setenv("UPDATE_TEST_FAIL", "")
	pending := filepath.Join(dir, "pending-update.json")
	if err := os.WriteFile(pending, []byte(`{"component":"hal"}`), 0600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, err := os.ReadFile(calls)
		if err != nil || string(data) != "recover\n" {
			t.Errorf("metadata fetched before recovery: %q, %v", data, err)
		}
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()
	b := &Bootstrap{cfg: &config.Config{MetadataURL: server.URL}, client: server.Client(), state: &state.State{Components: map[string]string{domain.OTAKeyHal: "9.0.0"}}, pendingUpdatePath: pending}
	if err := b.checkOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestNoPendingRecoveryDoesNotInvokeUpdater(t *testing.T) {
	dir, calls := setupUpdateAudit(t)
	b := &Bootstrap{pendingUpdatePath: filepath.Join(dir, "absent.json")}
	if err := b.recoverPendingUpdate(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(calls); !os.IsNotExist(err) {
		t.Fatalf("unexpected updater execution: %v", err)
	}
}

func TestLegacyHALRecoveryWithoutJournal(t *testing.T) {
	for _, liveExists := range []bool{false, true} {
		name := "missing-live"
		if liveExists {
			name = "installed-live"
		}
		t.Run(name, func(t *testing.T) {
			dir, calls := setupUpdateAudit(t)
			t.Setenv("UPDATE_TEST_FAIL", "")
			halPath := filepath.Join(dir, "hal")
			if liveExists {
				if err := os.Mkdir(halPath, 0755); err != nil {
					t.Fatal(err)
				}
			}
			if err := os.Mkdir(filepath.Join(dir, "hal.previous"), 0755); err != nil {
				t.Fatal(err)
			}
			if err := recoverPendingUpdateAt(context.Background(), filepath.Join(dir, "pending-update.json"), halPath); err != nil {
				t.Fatal(err)
			}
			data, err := os.ReadFile(calls)
			if liveExists {
				if !os.IsNotExist(err) {
					t.Fatalf("healthy installation must not trigger recovery: %q, %v", data, err)
				}
			} else if err != nil || string(data) != "recover\n" {
				t.Fatalf("legacy recovery calls = %q, %v", data, err)
			}
		})
	}
}

func TestLegacyHALRecoveryRequiresDirectoryBackup(t *testing.T) {
	dir, calls := setupUpdateAudit(t)
	if err := os.WriteFile(filepath.Join(dir, "hal.previous"), []byte("invalid backup"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := recoverPendingUpdateAt(context.Background(), filepath.Join(dir, "pending-update.json"), filepath.Join(dir, "hal")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(calls); !os.IsNotExist(err) {
		t.Fatalf("invalid backup must not trigger recovery: %v", err)
	}
}
