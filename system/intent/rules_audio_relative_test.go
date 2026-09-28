package intent

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"testing"
)

func TestRelativeVolumeRepeatedDown(t *testing.T) {
	current := 40
	var writes []int
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/audio/volume" {
			t.Errorf("unexpected path %s", r.URL.Path)
			w.WriteHeader(404)
			return
		}
		if r.Method == http.MethodGet {
			fmt.Fprintf(w, `{"volume":%d,"max_volume":40}`, current)
			return
		}
		var body struct {
			Volume int `json:"volume"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		current = body.Volume
		writes = append(writes, current)
	})
	for range 7 {
		result := MatchCommands("volume down")
		if result == nil || result.ExecutionFailed {
			t.Fatalf("result = %+v", result)
		}
	}
	if !reflect.DeepEqual(writes, []int{20, 10, 5, 2, 1, 0}) {
		t.Fatalf("writes = %v", writes)
	}
}

func TestRelativeVolumeBoundariesAndErrors(t *testing.T) {
	for _, tt := range []struct {
		name, command, body         string
		getStatus, postStatus, want int
		failure                     bool
	}{
		{"up step", "volume up", `{"volume":20,"max_volume":40}`, 200, 200, 24, false},
		{"up clamp", "volume up", `{"volume":39,"max_volume":40}`, 200, 200, 40, false},
		{"small ceiling", "volume up", `{"volume":0,"max_volume":3}`, 200, 200, 1, false},
		{"ceiling", "volume up", `{"volume":40,"max_volume":40}`, 200, 200, -1, false},
		{"above ceiling", "volume up", `{"volume":41,"max_volume":40}`, 200, 200, -1, false},
		{"zero ceiling", "volume up", `{"volume":0,"max_volume":0}`, 200, 200, -1, false},
		{"zero", "volume down", `{"volume":0,"max_volume":40}`, 200, 200, -1, false},
		{"legacy ceiling", "volume up", `{"volume":20}`, 200, 200, 30, false},
		{"read error", "volume down", `{}`, 503, 200, -1, true},
		{"missing", "volume down", `{}`, 200, 200, -1, true},
		{"null", "volume down", `{"volume":null}`, 200, 200, -1, true},
		{"negative", "volume down", `{"volume":-1}`, 200, 200, -1, true},
		{"overflow", "volume down", `{"volume":101}`, 200, 200, -1, true},
		{"fraction", "volume down", `{"volume":1.5}`, 200, 200, -1, true},
		{"invalid ceiling", "volume up", `{"volume":20,"max_volume":101}`, 200, 200, -1, true},
		{"malformed", "volume down", `{`, 200, 200, -1, true},
		{"write error", "volume down", `{"volume":20,"max_volume":40}`, 200, 503, 10, true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			got := -1
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodGet {
					w.WriteHeader(tt.getStatus)
					if got >= 0 && tt.postStatus == 200 {
						fmt.Fprintf(w, `{"volume":%d}`, got)
					} else {
						fmt.Fprint(w, tt.body)
					}
					return
				}
				var body struct {
					Volume int `json:"volume"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				got = body.Volume
				w.WriteHeader(tt.postStatus)
			})
			result := MatchCommands(tt.command)
			if result == nil || result.ExecutionFailed != tt.failure || got != tt.want {
				t.Fatalf("result=%+v write=%d want=%d failure=%v", result, got, tt.want, tt.failure)
			}
			if tt.failure && (result.TTSText == "Volume up!" || result.TTSText == "Volume down!") {
				t.Fatalf("false success: %+v", result)
			}
			if tt.want == -1 && !tt.failure && (result.TTSText == "Volume up!" || result.TTSText == "Volume down!") {
				t.Fatalf("false change: %+v", result)
			}
		})
	}
}

func TestRelativeVolumeVerifiesWrite(t *testing.T) {
	for _, failure := range []string{"ignored", "readback_error"} {
		t.Run(failure, func(t *testing.T) {
			written := false
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodPost {
					written = true
					return
				}
				if written && failure == "readback_error" {
					w.WriteHeader(503)
					return
				}
				fmt.Fprint(w, `{"volume":20,"max_volume":40}`)
			})
			result := MatchCommands("volume down")
			if result == nil || !result.ExecutionFailed || result.TTSText == "Volume down!" {
				t.Fatalf("result=%+v", result)
			}
		})
	}
}
