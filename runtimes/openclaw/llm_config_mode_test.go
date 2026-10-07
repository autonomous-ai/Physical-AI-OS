package openclaw

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sync/atomic"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

func TestRuntimeManagedLLMPreservesSelection(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		t.Error("runtime-managed LLM must not fetch the OS model catalog")
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	dir := t.TempDir()
	original := []byte(`{"models":{"providers":{"native":{"apiKey":"native-key","models":[{"id":"native-model","reasoning":false}]}}},"agents":{"defaults":{"model":{"primary":"native/native-model"},"imageModel":{"primary":"native/vision"},"thinkingDefault":"high","models":{"native/native-model":{"params":{"fastMode":false}}}}}}`)
	path := filepath.Join(dir, "openclaw.json")
	if err := os.WriteFile(path, original, 0600); err != nil {
		t.Fatal(err)
	}
	s := &OpenclawService{config: &config.Config{OpenclawConfigDir: dir, LLMConfigMode: "runtime", LLMBaseURL: server.URL, LLMAPIKey: "os-key", LLMModel: "os-model"}}
	if changed, err := s.ensureProviderConfig(); err != nil || changed {
		t.Fatalf("ensureProviderConfig: changed=%v err=%v", changed, err)
	}
	if changed, err := s.SyncModelsFromAPI(); err != nil || changed {
		t.Fatalf("SyncModelsFromAPI: changed=%v err=%v", changed, err)
	}
	if err := s.RefreshModelsConfig(); err != nil {
		t.Fatal(err)
	}
	if err := s.UpdatePrimaryModel("different"); err != nil {
		t.Fatal(err)
	}
	s.syncPrimaryFromFile()
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(original) {
		t.Fatal("native config changed during LLM synchronization")
	}
	if s.config.LLMModel != "os-model" {
		t.Fatal("native watcher changed saved OS model")
	}
	if _, err := s.ensureAgentDefaults(); err != nil {
		t.Fatal(err)
	}
	got, err = os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var before, after map[string]any
	if err = json.Unmarshal(original, &before); err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(got, &after); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(before["models"], after["models"]) {
		t.Fatal("onboarding changed native provider configuration")
	}
	bd := before["agents"].(map[string]any)["defaults"].(map[string]any)
	ad := after["agents"].(map[string]any)["defaults"].(map[string]any)
	for _, key := range []string{"model", "imageModel", "models", "thinkingDefault"} {
		if !reflect.DeepEqual(bd[key], ad[key]) {
			t.Errorf("onboarding changed native %s", key)
		}
	}
	if ad["bootstrapMaxChars"] != float64(bootstrapMaxChars) {
		t.Error("onboarding no longer maintains workspace bootstrap settings")
	}
	heartbeat, _ := ad["heartbeat"].(map[string]any)
	if heartbeat["every"] != "0m" || heartbeat["target"] != "none" || heartbeat["isolatedSession"] != true {
		t.Error("runtime-managed LLM bypassed the silent heartbeat policy")
	}
	if requests.Load() != 0 {
		t.Fatalf("unexpected catalog requests: %d", requests.Load())
	}
}

func TestExplicitOSRefreshRecreatesProviderAndSelectsOSModel(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"id":"os-model"}]}`))
	}))
	defer server.Close()
	dir := t.TempDir()
	// Stub both restart paths so the test never touches host services.
	for _, name := range []string{"openclaw", "systemctl"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("#!/bin/sh\nexit 0\n"), 0700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	path := filepath.Join(dir, "openclaw.json")
	if err := os.WriteFile(path, []byte(`{"agents":{"defaults":{"model":{"primary":"native/native-model"}}}}`), 0600); err != nil {
		t.Fatal(err)
	}
	s := &OpenclawService{config: &config.Config{OpenclawConfigDir: dir, LLMConfigMode: "os", LLMBaseURL: server.URL, LLMAPIKey: "os-key", LLMModel: "os-model"}}
	if err := s.RefreshModelsConfig(); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var cfg map[string]any
	if err = json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	if got := extractPrimaryModel(cfg); got != "autonomous/os-model" {
		t.Fatalf("primary=%q", got)
	}
	provider, ok := autonomousProviderMap(cfg)
	if !ok {
		t.Fatal("removed OS provider was not recreated")
	}
	if provider["apiKey"] != "os-key" || provider["baseUrl"] != server.URL {
		t.Fatal("OS credentials were not restored")
	}
}

func TestRuntimeManagedPrimaryWatcherDoesNotMirrorOSProvider(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "openclaw.json"), []byte(`{"agents":{"defaults":{"model":{"primary":"autonomous/manually-selected"}}}}`), 0600); err != nil {
		t.Fatal(err)
	}
	s := &OpenclawService{config: &config.Config{OpenclawConfigDir: dir, LLMConfigMode: "runtime", LLMModel: "saved-os-model"}}
	s.syncPrimaryFromFile()
	if s.config.LLMModel != "saved-os-model" {
		t.Fatal("native mode mirrored a manually selected model back into OS config")
	}
}
