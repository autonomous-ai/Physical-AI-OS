package openclaw

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestIsAutonomousEndpoint(t *testing.T) {
	cases := []struct {
		url  string
		want bool
	}{
		{"https://campaign-api.autonomous.ai/api/v1/ai/v1", true},
		{"https://autonomous.ai/v1", true},
		{"https://campaign-api.staging.autonomousdev.xyz/api/v1/ai/v1", true},
		{"", true},
		{"::not a url::", true},
		{"not-a-url-either", true},
		{"http://192.168.1.42:11434/v1", false},
		{"http://localhost:11434/v1", false},
		{"https://openrouter.ai/api/v1", false},
		{"https://autonomous.ai.evil.example.com/v1", false},
	}
	for _, c := range cases {
		if got := isAutonomousEndpoint(c.url); got != c.want {
			t.Errorf("isAutonomousEndpoint(%q) = %v, want %v", c.url, got, c.want)
		}
	}
}

func TestModelsEndpoint(t *testing.T) {
	cases := map[string]string{
		"http://localhost:11434/v1":        "http://localhost:11434/v1/models",
		"http://localhost:11434/v1/":       "http://localhost:11434/v1/models",
		"http://localhost:11434/v1/models": "http://localhost:11434/v1/models",
		"https://openrouter.ai/api/v1":     "https://openrouter.ai/api/v1/models",
	}
	for in, want := range cases {
		if got := modelsEndpoint(in); got != want {
			t.Errorf("modelsEndpoint(%q) = %q, want %q", in, got, want)
		}
	}
}

// A BYO OpenAI-compatible endpoint (Ollama, vLLM, LM Studio) supplies its own model list.
func TestResolveModels_OpenAIShape(t *testing.T) {
	var gotPath, gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotAuth = r.URL.Path, r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"object":"list","data":[{"id":"qwen3:8b"},{"id":"llama3.2:3b"}]}`))
	}))
	defer srv.Close()

	resp, byo, err := resolveModels(context.Background(), srv.URL+"/v1", "sk-local")
	if err != nil {
		t.Fatalf("resolveModels: %v", err)
	}
	if !byo {
		t.Error("want byo=true for a non-Autonomous base URL")
	}
	if gotPath != "/v1/models" {
		t.Errorf("asked %q, want /v1/models", gotPath)
	}
	if gotAuth != "Bearer sk-local" {
		t.Errorf("Authorization = %q, want the configured key", gotAuth)
	}
	if len(resp.Models) != 2 || resp.Models[0].Key != "qwen3:8b" {
		t.Fatalf("models = %+v", resp.Models)
	}
	if resp.DefaultModel != "qwen3:8b" {
		t.Errorf("DefaultModel = %q, want the first listed model", resp.DefaultModel)
	}
	if resp.API != "openai-completions" {
		t.Errorf("API = %q, want openai-completions", resp.API)
	}
}

// A proxy may serve our own catalog shape verbatim; take it as-is.
func TestResolveModels_OurCatalogShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"count":1,"default_model":"my-model","api":"anthropic-messages",
			"models":[{"key":"my-model","name":"My Model","reasoning":true}]}`))
	}))
	defer srv.Close()

	resp, byo, err := resolveModels(context.Background(), srv.URL+"/v1", "")
	if err != nil {
		t.Fatalf("resolveModels: %v", err)
	}
	if !byo {
		t.Error("want byo=true")
	}
	if len(resp.Models) != 1 || resp.Models[0].Name != "My Model" {
		t.Fatalf("models = %+v", resp.Models)
	}
	if !resp.Models[0].Reasoning {
		t.Error("our catalog shape should survive verbatim, reasoning included")
	}
}

// A BYO endpoint that cannot list models must report the error rather than silently advertising models it does not serve — the caller falls back.
func TestResolveModels_ByoErrorIsReported(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	_, byo, err := resolveModels(context.Background(), srv.URL+"/v1", "")
	if err == nil {
		t.Fatal("want an error from a 404 models endpoint")
	}
	if !byo {
		t.Error("want byo=true so the caller can say which endpoint failed")
	}
}

func TestResolveModels_EmptyListIsAnError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"object":"list","data":[]}`))
	}))
	defer srv.Close()

	if _, _, err := resolveModels(context.Background(), srv.URL+"/v1", ""); err == nil {
		t.Fatal("an empty model list must be an error, not an empty provider entry")
	}
}
