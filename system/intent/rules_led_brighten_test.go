package intent

import (
	"encoding/json"
	"net/http"
	"testing"
)

func TestBrightenReadbackAndLimits(t *testing.T) {
	for _, tc := range []struct {
		name             string
		initial, applied [3]int
		failure, noWrite bool
	}{
		{"relative hue", [3]int{100, 50, 200}, [3]int{125, 62, 250}, false, false},
		{"dark start", [3]int{}, [3]int{32, 27, 20}, false, false},
		{"rounding at low level", [3]int{1, 0, 1}, [3]int{2, 0, 2}, false, false},
		{"RGB maximum", [3]int{255, 100, 0}, [3]int{}, false, true},
		{"safety clamp", [3]int{100, 50, 200}, [3]int{110, 55, 220}, false, false},
		{"ignored write", [3]int{100, 50, 200}, [3]int{100, 50, 200}, true, false},
		{"wrong hue", [3]int{100, 50, 200}, [3]int{0, 0, 250}, true, false},
		{"unexpected brighter state", [3]int{100, 50, 200}, [3]int{255, 255, 255}, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			color, writes := tc.initial, 0
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method == "GET" {
					_ = json.NewEncoder(w).Encode(map[string]any{"color": color})
					return
				}
				if r.URL.Path != "/led/solid" {
					t.Errorf("unexpected action %s", r.URL.Path)
				}
				writes++
				color = tc.applied
			})
			got := brightenCurrentLight()
			if got.ExecutionFailed != tc.failure || (writes == 0) != tc.noWrite || got.LEDChanged != (!tc.failure && !tc.noWrite) {
				t.Fatalf("result=%+v writes=%d", got, writes)
			}
		})
	}
}

func TestBrightenFailuresAndSharedLock(t *testing.T) {
	for _, mode := range []string{"read", "invalid read", "write", "verify"} {
		t.Run(mode, func(t *testing.T) {
			writes := 0
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method == "POST" {
					writes++
					if mode == "write" {
						w.WriteHeader(503)
					}
					return
				}
				if mode == "read" || mode == "verify" && writes > 0 {
					w.WriteHeader(503)
					return
				}
				if mode == "invalid read" {
					_, _ = w.Write([]byte(`{"color":[-1,0,0]}`))
					return
				}
				_, _ = w.Write([]byte(`{"color":[100,50,200]}`))
			})
			if got := brightenCurrentLight(); !got.ExecutionFailed || got.LEDChanged {
				t.Fatalf("false success: %+v", got)
			}
			if (mode == "read" || mode == "invalid read") && writes != 0 {
				t.Fatal("wrote without valid state")
			}
		})
	}
	dimMu.Lock()
	got := brightenCurrentLight()
	dimMu.Unlock()
	if !got.ExecutionFailed {
		t.Fatal("brighten must share the dim adjustment lock")
	}
}

func TestBrightenRepeatedlyUsesCurrentColor(t *testing.T) {
	color := [3]int{100, 50, 200}
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			_ = json.NewEncoder(w).Encode(map[string]any{"color": color})
			return
		}
		var body struct {
			Color [3]int `json:"color"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		color = body.Color
	})
	for _, want := range [][3]int{{125, 62, 250}, {127, 63, 255}} {
		if got := brightenCurrentLight(); got.ExecutionFailed || color != want {
			t.Fatalf("result=%+v color=%v want=%v", got, color, want)
		}
	}
}
