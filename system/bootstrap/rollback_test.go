package bootstrap

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/bootstrap/config"
	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/domain"
)

func setupUpdateAudit(t *testing.T) (string, string) {
	t.Helper()
	dir := t.TempDir()
	bin := filepath.Join(dir, "bin")
	if err := os.Mkdir(bin, 0755); err != nil {
		t.Fatal(err)
	}
	calls := filepath.Join(dir, "calls")
	t.Setenv("UPDATE_TEST_CALLS", calls)
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	for name, body := range map[string]string{
		"software-update": "#!/bin/sh\nprintf '%s\\n' \"$1\" >> \"$UPDATE_TEST_CALLS\"\n[ \"$1\" != \"$UPDATE_TEST_FAIL\" ]\n",
		"os-server":       "#!/bin/sh\necho 1.0.0\n",
	} {
		if err := os.WriteFile(filepath.Join(bin, name), []byte(body), 0755); err != nil {
			t.Fatal(err)
		}
	}
	devices := filepath.Join(dir, "devices")
	profile := filepath.Join(devices, "ota-test")
	if err := os.MkdirAll(profile, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(profile, "VERSION"), []byte("1.0.0"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(profile, "ROBOT.md"), []byte("---\ncapabilities: {}\n---\n"), 0644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DEVICES_DIR", devices)
	t.Setenv("DEVICE_TYPE", "ota-test")
	original := scheduleOTAErrorRestore
	scheduleOTAErrorRestore = func(time.Duration, func()) {}
	t.Cleanup(func() { scheduleOTAErrorRestore = original })
	return dir, calls
}

func TestReconcileReloadsRollbackRulesWithoutRestart(t *testing.T) {
	dir, calls := setupUpdateAudit(t)
	t.Setenv("UPDATE_TEST_FAIL", domain.OTAKeyOSServer)
	path := filepath.Join(dir, "bootstrap.json")
	b := &Bootstrap{cfg: &config.Config{}, state: &state.State{Components: map[string]string{}}, rollbackConfigPath: path}
	target := domain.OTAComponent{Version: "2.0.0"}
	write := func(body string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
	// Rollback happens after this Bootstrap instance has already been constructed.
	write(`{"rollback_versions":{"os-server":"2.0.0"}}`)
	if updated, err := b.reconcile(context.Background(), domain.OTAKeyOSServer, target); err != nil || updated {
		t.Fatalf("blocked update = %v, %v", updated, err)
	}
	if _, err := os.Stat(calls); !os.IsNotExist(err) {
		t.Fatalf("blocked updater was invoked: %v", err)
	}
	if len(b.cfg.RollbackVersions) != 0 {
		t.Fatal("shared startup config was mutated")
	}
	// Removing a block also takes effect without mutating/restarting bootstrap.
	write(`{"rollback_versions":{}}`)
	if _, err := b.reconcile(context.Background(), domain.OTAKeyOSServer, target); err == nil {
		t.Fatal("expected fake updater failure after block removal")
	}
	data, err := os.ReadFile(calls)
	if err != nil || string(data) != "os-server\n" {
		t.Fatalf("updater calls = %q, %v", data, err)
	}
}

func TestReconcileRejectsUnreadableRollbackRules(t *testing.T) {
	dir, calls := setupUpdateAudit(t)
	path := filepath.Join(dir, "bootstrap.json")
	if err := os.WriteFile(path, []byte(`{"rollback_versions":`), 0600); err != nil {
		t.Fatal(err)
	}
	b := &Bootstrap{cfg: &config.Config{}, state: &state.State{Components: map[string]string{}}, rollbackConfigPath: path}
	if _, err := b.reconcile(context.Background(), domain.OTAKeyOSServer, domain.OTAComponent{Version: "2.0.0"}); err == nil {
		t.Fatal("malformed rules must not allow an update")
	}
	if _, err := os.Stat(calls); !os.IsNotExist(err) {
		t.Fatal("updater invoked despite malformed rollback rules")
	}
}

func TestCheckOnceGatesDeviceAfterCoreUpdateFailure(t *testing.T) {
	for _, failed := range []string{domain.OTAKeyHal, domain.OTAKeyOSServer, domain.OTAKeyWeb} {
		t.Run(failed, func(t *testing.T) {
			dir, calls := setupUpdateAudit(t)
			t.Setenv("UPDATE_TEST_FAIL", failed)
			metadata := map[string]any{failed: domain.OTAComponent{Version: "2.0.0"}, "devices": map[string]domain.OTAComponent{"ota-test": {Version: "2.0.0"}}}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if err := json.NewEncoder(w).Encode(metadata); err != nil {
					t.Error(err)
				}
			}))
			defer server.Close()
			b := &Bootstrap{
				cfg:                &config.Config{MetadataURL: server.URL, StateFile: filepath.Join(dir, "state.json")},
				client:             server.Client(),
				state:              &state.State{Components: map[string]string{failed: "1.0.0"}},
				rollbackConfigPath: filepath.Join(dir, "missing-config.json"),
				pendingUpdatePath:  filepath.Join(dir, "pending-update.json"),
			}
			err := b.checkOnce(context.Background())
			data, readErr := os.ReadFile(calls)
			if readErr != nil {
				t.Fatalf("read updater calls: %v (checkOnce error: %v)", readErr, err)
			}
			if failed == domain.OTAKeyWeb {
				if err != nil {
					t.Fatal(err)
				}
				if !strings.Contains(string(data), "device\n") {
					t.Fatalf("unrelated web failure should not block device: %q", data)
				}
			} else {
				if err == nil {
					t.Fatal("core update failure must be returned")
				}
				if string(data) != failed+"\n" {
					t.Fatalf("device updater ran after prerequisite failure: %q", data)
				}
				if _, ok := b.state.Components[domain.OTAKeyDevice]; ok {
					t.Fatal("skipped device update must not be recorded")
				}
			}
		})
	}
}
