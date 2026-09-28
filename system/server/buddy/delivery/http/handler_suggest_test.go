package http

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/buddy"
	buddyjev "go.autonomous.ai/os/system/buddy/jev"
)

func treeResponse(id string) json.RawMessage {
	raw, _ := json.Marshal(map[string]any{"id": id, "ok": true, "result": map[string]any{
		"snapshot_id": "snapshot-1", "frontmost": true, "truncated": false, "expires_in_ms": 30000,
		"nodes": []map[string]any{{"ref": "button-1", "role": "AXButton", "title": "Search", "enabled": true,
			"actions": []string{"press"}, "secure": false}},
	}})
	return raw
}

func TestSuggestionFreshObservationOnly(t *testing.T) {
	parent, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	calls := 0
	result := executeSuggestion(parent, suggestionRequest{Goal: "Focus search", App: "Safari"}, buddyjev.Options{Enabled: true},
		func(ctx context.Context, cmd buddy.Command) (json.RawMessage, error) {
			calls++
			if cmd.Action != "get_ui_tree" || cmd.Params["app"] != "Safari" || cmd.TimeoutMs != 5000 {
				t.Fatalf("unexpected command: %+v", cmd)
			}
			if deadline, ok := ctx.Deadline(); !ok || time.Until(deadline) > 2*time.Second {
				t.Fatal("caller deadline not retained")
			}
			return treeResponse(cmd.ID), nil
		}, func(ctx context.Context, goal string, tree buddyjev.Tree, opts buddyjev.Options) buddyjev.Result {
			if goal != "Focus search" || tree.SnapshotID != "snapshot-1" || !opts.Enabled {
				t.Fatal("lost goal/snapshot")
			}
			return buddyjev.Result{Reason: "suggested", Suggestion: &buddyjev.Suggestion{SnapshotID: tree.SnapshotID, Ref: "button-1", UIAction: "press"}}
		})
	if calls != 1 || result.Suggestion == nil || result.Suggestion.Ref != "button-1" {
		t.Fatalf("expected one observation and no mutation: %+v calls=%d", result, calls)
	}
}

func TestSuggestionOffOrCancelledDoesNotObserve(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		ctx, cancel := context.WithCancel(context.Background())
		if enabled {
			cancel()
		}
		result := executeSuggestion(ctx, suggestionRequest{Goal: "Search"}, buddyjev.Options{Enabled: enabled},
			func(context.Context, buddy.Command) (json.RawMessage, error) {
				t.Fatal("must not observe")
				return nil, nil
			},
			func(context.Context, string, buddyjev.Tree, buddyjev.Options) buddyjev.Result {
				t.Fatal("must not infer")
				return buddyjev.Result{}
			})
		cancel()
		if result.Suggestion != nil {
			t.Fatal("unexpected suggestion")
		}
	}
}

func TestSuggestionInvalidObservationDefers(t *testing.T) {
	for _, scenario := range []string{"id", "error", "expired", "invalid_ttl", "missing_secure", "missing_truncated", "oversize", "json", "dispatch", "cancelled"} {
		t.Run(scenario, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			result := executeSuggestion(ctx, suggestionRequest{Goal: "Search"}, buddyjev.Options{Enabled: true},
				func(_ context.Context, cmd buddy.Command) (json.RawMessage, error) {
					switch scenario {
					case "id":
						return treeResponse("wrong"), nil
					case "error":
						return json.RawMessage(`{"id":"` + cmd.ID + `","ok":false}`), nil
					case "expired":
						return json.RawMessage(strings.Replace(string(treeResponse(cmd.ID)), `"expires_in_ms":30000`, `"expires_in_ms":0`, 1)), nil
					case "invalid_ttl":
						time.Sleep(3 * time.Millisecond)
						return json.RawMessage(strings.Replace(string(treeResponse(cmd.ID)), `"expires_in_ms":30000`, `"expires_in_ms":30001`, 1)), nil
					case "missing_secure":
						return json.RawMessage(strings.Replace(string(treeResponse(cmd.ID)), `"secure":false,`, ``, 1)), nil
					case "missing_truncated":
						return json.RawMessage(strings.Replace(string(treeResponse(cmd.ID)), `,"truncated":false`, ``, 1)), nil
					case "oversize":
						return bytes.Repeat([]byte("x"), (1<<20)+1), nil
					case "json":
						return json.RawMessage(`{`), nil
					case "dispatch":
						return nil, errors.New("offline")
					case "cancelled":
						cancel()
						return treeResponse(cmd.ID), nil
					}
					return nil, nil
				}, func(context.Context, string, buddyjev.Tree, buddyjev.Options) buddyjev.Result {
					t.Fatal("invalid observation reached inference")
					return buddyjev.Result{}
				})
			if result.Suggestion != nil || result.Reason == "" {
				t.Fatalf("expected fallback: %+v", result)
			}
		})
	}
}

func TestSuggestHTTPValidationAndEnabledWithoutDependencies(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := gin.New()
	handler := BuddyHandler{}
	router.POST("/api/buddy/suggest", handler.Suggest)
	for _, body := range []string{`{}`, `{"goal":" "}`, `{"goal":"find","app":" "}`, `{"goal":4}`, `{"goal":"` + strings.Repeat("x", 2001) + `"}`, `{"goal":"find","app":"` + strings.Repeat("x", 257) + `"}`} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/api/buddy/suggest", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		router.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("invalid request %d %s", w.Code, w.Body.String())
		}
	}
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/buddy/suggest", strings.NewReader(`{"goal":"Tìm nút tìm kiếm"}`))
	req.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"suggestion":null`) || !strings.Contains(w.Body.String(), `"reason":"unavailable"`) {
		t.Fatalf("enabled handler without dependencies: %d %s", w.Code, w.Body.String())
	}
}
