package intent

import (
	"encoding/json"
	"net/http"
	"testing"
)

func TestDimRepeatedlyPreservesColorAndDoesNotRelight(t *testing.T) {
	color := [3]int{100, 50, 200}
	writes := 0
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			_ = json.NewEncoder(w).Encode(map[string]any{"color": color})
			return
		}
		if r.URL.Path != "/led/solid" {
			t.Fatalf("unexpected action %s", r.URL.Path)
		}
		var body struct {
			Color [3]int `json:"color"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		color = body.Color
		writes++
	})
	for _, want := range [][3]int{{50, 25, 100}, {25, 12, 50}} {
		got := dimCurrentLight()
		if got.ExecutionFailed || color != want || !got.LEDChanged {
			t.Fatalf("got %+v color %v want %v", got, color, want)
		}
	}
	color = [3]int{1, 0, 1}
	if got := dimCurrentLight(); !got.LEDOff || color != [3]int{} {
		t.Fatalf("lowest level did not go dark: %+v %v", got, color)
	}
	count := writes
	if got := dimCurrentLight(); got.ExecutionFailed || writes != count || got.TTSText != "The light is already off." {
		t.Fatalf("off handling: %+v", got)
	}
}

func TestDimFailureDoesNotClaimSuccess(t *testing.T) {
	for _, mode := range []string{"read", "write", "ignored"} {
		t.Run(mode, func(t *testing.T) {
			writes := 0
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method == "GET" {
					if mode == "read" {
						w.WriteHeader(503)
						return
					}
					_, _ = w.Write([]byte(`{"color":[100,50,200]}`))
					return
				}
				writes++
				if mode == "write" {
					w.WriteHeader(503)
				}
			})
			got := dimCurrentLight()
			if !got.ExecutionFailed || got.LEDChanged || got.TTSText == "Dimmed." {
				t.Fatalf("false success: %+v", got)
			}
			if mode == "read" && writes != 0 {
				t.Fatal("wrote without reading state")
			}
		})
	}
}

func TestConcurrentAdjustmentDoesNotQueue(t *testing.T) {
	dimMu.Lock()
	result := dimCurrentLight()
	dimMu.Unlock()
	if !result.ExecutionFailed {
		t.Fatal("busy dim should fail promptly")
	}
	volumeAdjustmentMu.Lock()
	result = adjustVolume(false)
	volumeAdjustmentMu.Unlock()
	if !result.ExecutionFailed {
		t.Fatal("busy volume should fail promptly")
	}
}
