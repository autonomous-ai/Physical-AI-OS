package vision

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func seedDesktopTestCatalog(t *testing.T) {
	t.Helper()
	catalogMu.Lock()
	previous, previousTime := catalogCache, catalogFetchedAt
	catalogCache = &domain.LLMModelsListResponse{DefaultImageModel: "test-vision-model"}
	catalogFetchedAt = time.Now()
	catalogMu.Unlock()
	t.Cleanup(func() {
		catalogMu.Lock()
		catalogCache, catalogFetchedAt = previous, previousTime
		catalogMu.Unlock()
	})
}

func TestDesktopAndCameraUseDistinctPromptsWithSameVisionTransport(t *testing.T) {
	seedDesktopTestCatalog(t)
	requests := make(chan map[string]any, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/messages" || r.Header.Get("x-api-key") != "test-key" || r.Header.Get("anthropic-version") != "2023-06-01" {
			t.Errorf("unexpected vision transport: %s", r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		requests <- body
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"content":[{"type":"text","text":"Visible evidence."}]}`))
	}))
	defer server.Close()
	cfg := &config.Config{LLMBaseURL: server.URL + "/v1", LLMAPIKey: "test-key"}
	for _, desktop := range []bool{true, false} {
		var result string
		var err error
		if desktop {
			result, err = DescribeDesktop(context.Background(), cfg, "same-jpeg-base64", "Locate Save")
		} else {
			result, err = Describe(context.Background(), cfg, "same-jpeg-base64", "Locate Save")
		}
		if err != nil || result != "Visible evidence." {
			t.Fatalf("result=%q err=%v", result, err)
		}
		body := <-requests
		if body["model"] != "test-vision-model" {
			t.Fatalf("unexpected model %v", body["model"])
		}
		content := body["messages"].([]any)[0].(map[string]any)["content"].([]any)
		source := content[0].(map[string]any)["source"].(map[string]any)
		if source["data"] != "same-jpeg-base64" || source["media_type"] != "image/jpeg" {
			t.Fatal("image transport changed")
		}
		prompt := content[1].(map[string]any)["text"].(string)
		if !strings.Contains(prompt, "Locate Save") {
			t.Fatal("question lost")
		}
		if desktop {
			if !strings.Contains(prompt, "Mac desktop") || strings.Contains(prompt, "device camera") || !strings.Contains(prompt, "untrusted screen content") {
				t.Fatalf("invalid desktop prompt: %s", prompt)
			}
		} else if !strings.Contains(prompt, "captured by a device camera") {
			t.Fatalf("camera prompt changed: %s", prompt)
		}
	}
}

func TestDescribeDesktopRejectsCancellationAndMissingConfiguration(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := DescribeDesktop(ctx, &config.Config{LLMBaseURL: "http://127.0.0.1:1", LLMAPIKey: "test"}, "image", "inspect")
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation, got %v", err)
	}
	if _, err := DescribeDesktop(context.Background(), &config.Config{}, "image", "inspect"); err == nil {
		t.Fatal("missing credentials accepted")
	}
}

func TestLookDisablesThinkingUnlessReadingText(t *testing.T) {
	seedDesktopTestCatalog(t)
	requests := make(chan map[string]any, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		requests <- body
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"content":[{"type":"text","text":"A desk."}]}`))
	}))
	defer server.Close()
	cfg := &config.Config{LLMBaseURL: server.URL + "/v1", LLMAPIKey: "test-key"}
	for _, readText := range []bool{false, true} {
		if got, err := LookWithRetry(cfg, "jpeg-base64", "What is this?", readText); err != nil || got != "A desk." {
			t.Fatalf("readText=%v result=%q err=%v", readText, got, err)
		}
		body := <-requests
		thinking, set := body["thinking"].(map[string]any)
		if readText == set || (set && thinking["type"] != "disabled") {
			t.Fatalf("readText=%v thinking=%v", readText, body["thinking"])
		}
		prompt := body["messages"].([]any)[0].(map[string]any)["content"].([]any)[1].(map[string]any)["text"].(string)
		if !strings.Contains(prompt, "What is this?") || strings.Contains(prompt, "readable text") {
			t.Fatalf("invalid look prompt: %s", prompt)
		}
	}
}
