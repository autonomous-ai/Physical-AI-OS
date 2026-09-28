package http

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"image"
	"image/jpeg"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/buddy"
)

func captureResponse(t *testing.T, id string, edit func(map[string]any)) json.RawMessage {
	t.Helper()
	var jpegBytes bytes.Buffer
	if err := jpeg.Encode(&jpegBytes, image.NewRGBA(image.Rect(0, 0, 4, 3)), nil); err != nil {
		t.Fatal(err)
	}
	result := map[string]any{"image_b64": base64.StdEncoding.EncodeToString(jpegBytes.Bytes()), "mime": "image/jpeg", "width": 4, "height": 3,
		"display_id": 123, "image_to_global_points": map[string]any{"origin_x": -100, "origin_y": 0, "scale_x": 2, "scale_y": 2}}
	response := map[string]any{"id": id, "ok": true, "result": result}
	if edit != nil {
		edit(response)
	}
	raw, err := json.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestExecuteObservationPreservesContextAndStripsImage(t *testing.T) {
	dispatches, descriptions := 0, 0
	parent, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	result, err := executeObservation(parent, observationRequest{Question: "Locate the save button"},
		func(ctx context.Context, cmd buddy.Command) (json.RawMessage, error) {
			dispatches++
			if cmd.Action != "screenshot" || cmd.ID == "" || cmd.TimeoutMs != 15000 || cmd.Params["return_format"] != "base64" || cmd.Params["scale"] != .5 {
				t.Fatalf("unexpected command: %+v", cmd)
			}
			if deadline, ok := ctx.Deadline(); !ok || time.Until(deadline) > 10*time.Second {
				t.Fatal("lost caller deadline")
			}
			return captureResponse(t, cmd.ID, nil), nil
		}, func(ctx context.Context, encoded, question string) (string, error) {
			descriptions++
			if encoded == "" || question != "Locate the save button" {
				t.Fatal("missing image or question")
			}
			return " Save is visible at the top. ", nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if dispatches != 1 || descriptions != 1 || result.Description != "Save is visible at the top." {
		t.Fatalf("unexpected result: %+v", result)
	}
	if _, exists := result.Screenshot["image_b64"]; exists {
		t.Fatal("image leaked into observation response")
	}
	if result.Screenshot["image_to_global_points"] == nil {
		t.Fatal("lost screenshot geometry")
	}
}

func TestObservationRejectsBadCaptureBeforeDescribing(t *testing.T) {
	cases := map[string]func(map[string]any){
		"wrong id":   func(r map[string]any) { r["id"] = "other" },
		"failed":     func(r map[string]any) { r["ok"] = false; r["error"] = "permission denied" },
		"bad mime":   func(r map[string]any) { r["result"].(map[string]any)["mime"] = "image/png" },
		"bad base64": func(r map[string]any) { r["result"].(map[string]any)["image_b64"] = "???" },
		"not jpeg": func(r map[string]any) {
			r["result"].(map[string]any)["image_b64"] = base64.StdEncoding.EncodeToString([]byte("not an image"))
		},
		"mismatched dimensions": func(r map[string]any) { r["result"].(map[string]any)["width"] = 123 },
		"missing image":         func(r map[string]any) { delete(r["result"].(map[string]any), "image_b64") },
	}
	for name, edit := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := executeObservation(context.Background(), observationRequest{Question: "Inspect"},
				func(ctx context.Context, cmd buddy.Command) (json.RawMessage, error) {
					return captureResponse(t, cmd.ID, edit), nil
				},
				func(context.Context, string, string) (string, error) {
					t.Fatal("invalid image reached vision")
					return "", nil
				})
			if err == nil {
				t.Fatal("expected invalid capture error")
			}
		})
	}
}

func TestObservationValidationAndCancellationPreventDispatch(t *testing.T) {
	zero := uint32(0)
	badScale := .001
	for _, req := range []observationRequest{{Question: " "}, {Question: strings.Repeat("界", 2001)}, {Question: "Inspect", DisplayID: &zero}, {Question: "Inspect", Scale: &badScale}} {
		if _, err := executeObservation(context.Background(), req,
			func(context.Context, buddy.Command) (json.RawMessage, error) {
				t.Fatal("invalid input dispatched")
				return nil, nil
			}, nil); err == nil {
			t.Fatal("expected validation error")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := executeObservation(ctx, observationRequest{Question: "Inspect"},
		func(context.Context, buddy.Command) (json.RawMessage, error) {
			t.Fatal("cancelled input dispatched")
			return nil, nil
		}, nil)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation, got %v", err)
	}
}

func TestObservationPropagatesCancellationToVisionWithoutRetry(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	calls := 0
	_, err := executeObservation(ctx, observationRequest{Question: "Inspect"},
		func(ctx context.Context, cmd buddy.Command) (json.RawMessage, error) {
			return captureResponse(t, cmd.ID, nil), nil
		},
		func(ctx context.Context, _, _ string) (string, error) {
			calls++
			cancel()
			<-ctx.Done()
			return "", ctx.Err()
		})
	if calls != 1 || !errors.Is(err, context.Canceled) {
		t.Fatalf("calls=%d err=%v", calls, err)
	}
}

func TestObservationRejectsOversizedPayload(t *testing.T) {
	_, _, err := validateDesktopScreenshot(json.RawMessage(strings.Repeat(" ", base64.StdEncoding.EncodedLen(maxDesktopJPEG)+(64<<10)+1)), "id")
	if err == nil {
		t.Fatal("oversized payload accepted")
	}
}

func TestObserveHTTPRejectsInvalidRequestsBeforeUsingService(t *testing.T) {
	for _, body := range []string{`{}`, `{"question":" "}`, `{"question":"Inspect","scale":true}`, `{"question":"Inspect","display_id":4294967296}`, `{"question":"Inspect","scale":2}`} {
		writer := httptest.NewRecorder()
		ctx, _ := gin.CreateTestContext(writer)
		ctx.Request = httptest.NewRequest(http.MethodPost, "/api/buddy/observe", strings.NewReader(body))
		ctx.Request.Header.Set("Content-Type", "application/json")
		handler := &BuddyHandler{}
		handler.Observe(ctx)
		if writer.Code != http.StatusBadRequest {
			t.Fatalf("body=%s status=%d", body, writer.Code)
		}
		var envelope struct {
			Status  int    `json:"status"`
			Data    any    `json:"data"`
			Message string `json:"message"`
		}
		if err := json.Unmarshal(writer.Body.Bytes(), &envelope); err != nil {
			t.Fatal(err)
		}
		if envelope.Status != 0 || envelope.Data != nil || envelope.Message == "" {
			t.Fatalf("invalid error envelope: %+v", envelope)
		}
	}
}

func TestWindowObservationRequiresTargetedCapture(t *testing.T) {
	app, windowID := "Calendar", uint32(42)
	for _, scoped := range []bool{false, true} {
		described := false
		result, err := executeObservation(context.Background(), observationRequest{Question: "Read events", App: &app, WindowID: &windowID},
			func(ctx context.Context, cmd buddy.Command) (json.RawMessage, error) {
				if cmd.Params["app"] != app || cmd.Params["window_id"] != windowID || cmd.Params["display_id"] != nil || cmd.Params["scale"] != float64(1) {
					t.Fatalf("wrong target: %+v", cmd.Params)
				}
				return captureResponse(t, cmd.ID, func(r map[string]any) {
					if scoped {
						m := r["result"].(map[string]any)
						m["capture_scope"] = "window"
						m["window_id"] = 42
						m["window_bounds"] = map[string]any{"x": -100, "y": 0, "width": 8, "height": 6}
					}
				}), nil
			}, func(context.Context, string, string) (string, error) { described = true; return "Events", nil })
		if !scoped {
			if err == nil || described {
				t.Fatal("untargeted capture reached model")
			}
			continue
		}
		if err != nil || !described || result.Screenshot["window_id"] != float64(42) || result.Screenshot["window_bounds"] == nil {
			t.Fatalf("lost window metadata: %+v %v", result, err)
		}
	}
}

func TestWindowObservationRejectsAmbiguousTargetParams(t *testing.T) {
	app, blank, longApp := "Calendar", " ", strings.Repeat("a", 257)
	id, zero := uint32(42), uint32(0)
	for _, req := range []observationRequest{
		{Question: "Read", WindowID: &id}, {Question: "Read", App: &blank}, {Question: "Read", App: &longApp},
		{Question: "Read", App: &app, WindowID: &zero}, {Question: "Read", App: &app, DisplayID: &id},
	} {
		if req.validate() == nil {
			t.Fatalf("invalid target accepted: %+v", req)
		}
	}
}
