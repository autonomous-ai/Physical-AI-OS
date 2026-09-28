package intent

import (
	"fmt"
	"net/http"
	"testing"

	"go.autonomous.ai/os/system/lib/hal"
)

func TestSleepingLightCommandsAreBlocked(t *testing.T) {
	for _, body := range []string{`{"sleeping":true}`, `{}`, `{"sleeping":null}`, `broken`} {
		for _, command := range []string{"light on", "set the light red", "dim the light"} {
			t.Run(body+command, func(t *testing.T) {
				routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
					if r.URL.Path != "/emotion/status" || r.Method != "GET" {
						t.Errorf("unexpected action %s %s", r.Method, r.URL.Path)
					}
					fmt.Fprint(w, body)
				})
				result := MatchCommands(command)
				if result == nil || !result.ExecutionFailed || result.LEDChanged || result.LEDOff || result.Emotion != "" {
					t.Fatalf("result=%+v", result)
				}
			})
		}
	}
}

func TestFailedCommandNeverSignalsSuccess(t *testing.T) {
	c := command{rule: &rule{name: "test", exec: func(string) *Result {
		return &Result{ExecutionFailed: true, TTSText: "Done!", LEDChanged: true, LEDOff: true, Emotion: "happy"}
	}}}
	result := c.execute()
	if result.TTSText == "Done!" || result.LEDChanged || result.LEDOff || result.Emotion != "" {
		t.Fatalf("result=%+v", result)
	}
}

func TestGetColorRequiresThreeValidChannels(t *testing.T) {
	for _, body := range []string{`{}`, `{"color":null}`, `{"color":[]}`, `{"color":[1,2]}`, `{"color":[1,2,3,4]}`, `{"color":[null,2,3]}`, `{"color":[-1,2,3]}`, `{"color":[256,2,3]}`, `{"color":[1.5,2,3]}`} {
		t.Run(body, func(t *testing.T) {
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) })
			if color, err := hal.GetColor(); err == nil {
				t.Fatalf("accepted %s as %v", body, color)
			}
		})
	}
}
